"""执行引擎：用例执行、套件编排、后台任务队列。

设计要点（与"缺条件不算通过"原则一致）：

- 没有断言 → ``incomplete``，绝不因为"没报错"就算通过；
- 没有 ``base_url`` → ``incomplete``，提示配置环境；
- 请求没发出去/超时 → ``error``；
- 断言失败 → ``failed``；
- 用例被停用 → ``skipped``。

隔离与并发：

- 后台任务队列按项目串行（同一项目同一时刻只跑一个运行），不同项目可并行；
- 并发度 > 1 的套件内部按用例并行，但每个用例使用独立的变量解析器，
  运行级提取变量不跨用例共享（避免并发下的隐藏依赖）。
"""

from __future__ import annotations

import queue
import json
import hashlib
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Sequence

from .assertions import build_context, evaluate_all
from .config import PlatformConfig
from .db import vacuum_runs
from .errors import NotFoundError, QueueFullError
from .http_client import request as http_request
from .models import (
    CaseResult,
    CaseSpec,
    EnvironmentSpec,
    HttpResponse,
    RunResult,
    now_iso,
)
from .redact import is_secret_key, mask, redact_headers, redact_mapping, redact_url
from .repository import ProjectRecord, ProjectRegistry, Repository
from .variables import VAR_PATTERN, VariableResolver, apply_extractors, build_resolver, extract_value

ACTIVE_STATUSES = ("queued", "running")
TERMINAL_STATUSES = ("passed", "failed", "error", "incomplete", "canceled")

#: 执行日志里的终态文案，与 Web 端一致，避免同一个状态两种叫法
STATUS_LABELS = {
    "passed": "通过",
    "failed": "失败",
    "error": "错误",
    "incomplete": "未完成",
    "skipped": "跳过",
    "canceled": "已取消",
}

#: 执行日志里单条事件的最大字符数；响应体可能有几十 KB，日志只留够定位问题的片段
LOG_TEXT_LIMIT = 600


def _log_text(value: Any, limit: int = LOG_TEXT_LIMIT) -> str:
    """把任意值压成一行短文本，避免日志被响应体刷屏。"""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=False)
    else:
        text = str(value)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit] + "…（已截断）"
    return text


def _log_headers(headers: Any) -> str:
    """请求头统一渲染成 ``Key=Value``，值已在 request_preview 里脱敏。"""
    if not isinstance(headers, dict) or not headers:
        return ""
    return "；".join("%s=%s" % (name, value) for name, value in headers.items())


def _status_mark(status: str) -> str:
    return {
        "passed": "✓",
        "failed": "✗",
        "error": "⚠",
        "incomplete": "◻",
        "skipped": "⊘",
        "canceled": "⊘",
    }.get(str(status), "·")


@dataclass
class RunRequest:
    """一次运行的请求参数。"""

    project_id: str
    case_ids: List[int] = field(default_factory=list)
    suite_id: Optional[int] = None
    env_id: Optional[int] = None
    trigger: str = "manual"
    note: str = ""
    operator: str = ""
    request_id: str = ""
    variables: Dict[str, Any] = field(default_factory=dict)

    def signature(self) -> str:
        return "suite=%s;cases=%s;env=%s;vars=%s" % (
            self.suite_id,
            ",".join(str(item) for item in self.case_ids),
            self.env_id,
            sorted(self.variables.items()),
        )


class CaseRunner:
    """单条用例执行器。

    每个执行器持有自己的 Cookie Jar：同一次运行内的用例/前置步骤共享会话
    （典型场景是"登录一次，后续接口带上会话 Cookie"）；并发套件为每条用例
    单独创建执行器，避免共享会话造成的隐藏依赖。
    """

    def __init__(
        self,
        config: PlatformConfig,
        repository: Repository,
        project: Optional[ProjectRecord] = None,
        cookie_jar: Optional[Dict[str, str]] = None,
    ) -> None:
        self.config = config
        self.repo = repository
        self.project = project
        self.cookie_jar: Dict[str, str] = cookie_jar if cookie_jar is not None else {}

    def run_case(
        self,
        case: CaseSpec,
        environment: EnvironmentSpec,
        run_variables: Optional[Dict[str, Any]] = None,
        cancel_check: Optional[Any] = None,
    ) -> CaseResult:
        started_at = now_iso()
        started = time.perf_counter()
        result = CaseResult(case_id=case.id, case_name=case.name, started_at=started_at)

        def log(message: str) -> None:
            """执行日志：每条带时间戳，界面按原样逐行展示。"""
            result.logs.append("[%s] %s" % (now_iso(), message))

        def finish(status: str, error: str = "") -> CaseResult:
            result.status = status
            result.error = error
            result.duration_ms = int((time.perf_counter() - started) * 1000)
            attempts = "，尝试 %d 次" % result.attempts if result.attempts > 1 else ""
            log(
                "结果：%s%s，耗时 %dms%s"
                % (STATUS_LABELS.get(status, status), "（%s）" % error if error else "", result.duration_ms, attempts)
            )
            return result

        if not case.enabled:
            log("用例已停用，跳过执行")
            return finish("skipped")

        base_url = (case.request.base_url or environment.base_url or (self.project.base_url if self.project else "") or "").strip()
        log("开始执行用例 #%s %s" % (case.id if case.id is not None else "?", case.name))
        if not case.request.path:
            log("未配置请求路径，无法执行")
            return finish("incomplete", "用例未配置请求路径")
        if not case.assertions:
            log("未定义任何断言，按规则不能判定为通过")
            return finish("incomplete", "用例未定义任何断言，无法判定结果")
        if "://" not in case.request.path and not base_url:
            log("未配置 base_url（用例、环境、项目三处均为空）")
            return finish("incomplete", "未配置 base_url：请在用例、环境或项目中设置请求地址")
        log("环境 %s · base_url %s" % (environment.name or "未命名环境", base_url))

        resolver = build_resolver(
            dict(run_variables or {}),
            dict(case.variables or {}),
            dict(environment.variables or {}),
        )
        resolver.set("_case_started_epoch", int(time.time()) - 5)
        # 请求里的变量引用了但没定义时，直接说清楚，避免出现"莫名其妙的 400/401"
        unresolved = _unresolved_request_variables(case, resolver)
        if unresolved:
            log("以下变量未定义，将按原文发送（请在项目环境变量中补齐）：%s" % "、".join(unresolved))
        auth = case.request.auth
        if auth is not None and auth.type in ("inherit", "", None):
            inherited = environment.auth.to_dict()
            if inherited.get("type") not in ("inherit", "", None):
                from .models import AuthSpec

                case.request.auth = AuthSpec.from_dict(inherited)

        # 前置步骤
        if case.setup:
            log("前置步骤 %d 个" % len(case.setup))
        for index, step in enumerate(case.setup):
            if cancel_check is not None and cancel_check():
                log("运行已被取消，停止执行剩余步骤")
                return finish("canceled", "运行已被取消")
            step_name = step.name or ("前置步骤 %d" % (index + 1))
            log("▶ 前置步骤 %d/%d：%s" % (index + 1, len(case.setup), step_name))
            step_started = time.perf_counter()
            missing = [name for name in step.required_variables if resolver.get(name) in (None, "", "******")]
            if missing:
                log("步骤缺少环境变量：%s" % "、".join(missing))
                return finish("incomplete", "步骤「%s」缺少环境变量：%s" % (step.name, "、".join(missing)))
            timeout = max(0, min(int(step.poll_timeout_ms or 0), 300000))
            if timeout and step.request.method not in ("GET", "HEAD") and not step.poll_read_only:
                log("轮询步骤配置非法：非只读请求需要 poll_read_only=true")
                return finish("incomplete", "轮询步骤「%s」须为只读查询；POST 查询需明确 poll_read_only=true" % step.name)
            deadline = time.monotonic() + timeout / 1000
            attempt = 0
            while True:
                if cancel_check is not None and cancel_check():
                    return finish("canceled", "运行已被取消")
                attempt += 1
                poll_request = step.request
                if timeout:
                    remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
                    poll_request = replace(step.request, timeout_ms=min(step.request.timeout_ms or self.config.default_timeout_ms, remaining_ms))
                step_response = self._send(poll_request, base_url, resolver)
                # 提取与断言在临时变量层中执行，失败的轮询不污染后续请求。
                candidate = build_resolver(resolver.as_dict())
                extractors = [replace(item, expression=resolver.resolve_text(item.expression)) for item in step.extractors]
                extracted, warnings = apply_extractors(step_response, extractors, candidate)
                required_missing = [item.name for item in extractors if item.required and
                                    (not extract_value(step_response, item)[0] or candidate.get(item.name) in (None, ""))]
                checks = self._evaluate(step.assertions, step_response, candidate)
                status = "error" if step_response.error else ("failed" if required_missing or any(not item.passed for item in checks) else "passed")
                step_record = {
                    "name": step.name or ("前置步骤 %d" % (index + 1)), "status": status,
                    "attempts": attempt, "duration_ms": int((time.perf_counter() - step_started) * 1000),
                    "request": dict(step_response.request_preview), "response": self._record_response(step_response),
                    "extracted": extracted, "warnings": warnings,
                    "assertions": [item.to_dict() for item in checks],
                }
                self._log_exchange(log, step_record["request"], step_record["response"])
                if status == "passed":
                    apply_extractors(step_response, extractors, resolver)
                    break
                if step_response.error or (step_response.status or 0) >= 400 or time.monotonic() >= deadline:
                    break
                remaining = deadline - time.monotonic()
                pause = min(max(10, int(step.poll_interval_ms or 1000)) / 1000, remaining)
                until = time.monotonic() + pause
                while time.monotonic() < until:
                    if cancel_check is not None and cancel_check():
                        return finish("canceled", "运行已被取消")
                    time.sleep(min(0.1, max(0, until - time.monotonic())))
            result.steps.append(step_record)
            log(
                "  %s 步骤「%s」%s（第 %d 次，%dms）"
                % (
                    _status_mark(step_record["status"]),
                    step_name,
                    STATUS_LABELS.get(step_record["status"], step_record["status"]),
                    attempt,
                    step_record["duration_ms"],
                )
            )
            if step_record["status"] != "passed" and not step.continue_on_failure:
                return finish("incomplete", "前置步骤失败：%s" % step_record["name"])
            if step_record["status"] != "passed":
                log("  按步骤配置继续执行后续请求")

        attempts = max(1, int(case.retries or 0) + 1)
        if attempts > 1:
            log("允许重试：最多 %d 次" % attempts)
        last_response: Optional[HttpResponse] = None
        for attempt in range(1, attempts + 1):
            if cancel_check is not None and cancel_check():
                log("运行已被取消，停止执行")
                return finish("canceled", "运行已被取消")
            result.attempts = attempt
            if attempt > 1:
                log("第 %d 次尝试" % attempt)
            response = self._send(case.request, base_url, resolver)
            last_response = response
            result.request = dict(response.request_preview)
            # folder 供报告按分组归类（JUnit 的 classname）
            result.request["folder"] = case.folder or "未分组"
            result.response = self._record_response(response)
            self._log_exchange(log, result.request, result.response)

            if response.error:
                result.assertions = []
                if attempt < attempts:
                    log("第 %d 次请求失败：%s，重试" % (attempt, response.error))
                    continue
                return finish("error", response.error)

            assertion_results = self._evaluate(case.assertions, response, resolver)
            result.assertions = assertion_results
            self._log_assertions(log, assertion_results)

            if case.extractors:
                extracted, warnings = apply_extractors(response, case.extractors, resolver)
                result.extracted.update(extracted)
                for warning in warnings:
                    log(warning)
                if extracted:
                    log("提取变量：%s" % _log_text(extracted, 200))

            failed = [item for item in assertion_results if not item.passed]
            if not failed:
                return finish("passed")
            if attempt < attempts:
                log("第 %d 次断言失败 %d 条，重试" % (attempt, len(failed)))
                continue
            return finish("failed", "；".join(item.describe or item.message for item in failed[:3]))

        # 理论上不可达
        if last_response is not None and last_response.error:
            return finish("error", last_response.error)
        return finish("failed")

    def _send(self, request_spec, base_url: str, resolver: VariableResolver) -> HttpResponse:
        resolver.set("_now_epoch", int(time.time()) + 1)
        return http_request(request_spec, self.config, base_url=base_url, resolver=resolver, cookie_jar=self.cookie_jar)

    @staticmethod
    def _log_exchange(log: Any, request: Any, response: Any) -> None:
        """把「发了什么 / 回了什么」写进执行日志。

        两个入参都用已脱敏的预览/记录字典，日志里不会出现真实凭据。
        """
        preview = request if isinstance(request, dict) else {}
        log("→ %s %s" % (preview.get("method") or "GET", preview.get("url") or ""))
        headers = _log_headers(preview.get("headers"))
        if headers:
            log("  请求头：%s" % headers)
        body = _log_text(preview.get("body"))
        if body:
            log("  请求体：%s" % body)
        record = response if isinstance(response, dict) else {}
        duration = int(record.get("duration_ms") or 0)
        if record.get("error"):
            log("← 请求失败：%s（%dms）" % (record.get("error"), duration))
            return
        log(
            "← HTTP %s（%dms，%d 字节%s）"
            % (record.get("status"), duration, int(record.get("size_bytes") or 0), "，响应体已截断" if record.get("truncated") else "")
        )
        text = _log_text(record.get("body_text"))
        if text:
            log("  响应体：%s" % text)

    @staticmethod
    def _log_assertions(log: Any, results: Sequence[Any]) -> None:
        """逐条记录断言，失败项带上实际值与期望值。"""
        total = len(results)
        if not total:
            log("没有断言可执行")
            return
        for index, item in enumerate(results, start=1):
            detail = item.describe or "%s %s" % (item.type, item.operator)
            if not item.passed and item.message:
                detail = "%s（%s）" % (detail, item.message)
            log("  %s 断言 %d/%d：%s" % (_status_mark("passed" if item.passed else "failed"), index, total, detail))

    def _evaluate(self, assertions, response, resolver):
        resolved = [replace(item, expected=resolver.resolve_value(item.expected),
                            path=resolver.resolve_text(item.path)) for item in assertions]
        results = evaluate_all(resolved, build_context(response, resolver.as_dict()))
        for item in results:
            if is_secret_key(item.target):
                item.actual, item.expected = mask(item.actual), mask(item.expected)
                item.message = "敏感字段断言%s（取值已脱敏）" % ("通过" if item.passed else "失败")
                item.describe = "%s[%s] %s（取值已脱敏）" % (item.type, item.target, item.operator)
            else:
                item.actual = redact_mapping(item.actual) if isinstance(item.actual, (dict, list)) else item.actual
                item.expected = redact_mapping(item.expected) if isinstance(item.expected, (dict, list)) else item.expected
        return results

    def _record_response(self, response: HttpResponse) -> Dict[str, Any]:
        """落库前的响应记录：脱敏 + 截断，保留足够定位问题的信息。"""
        body = (json.dumps(redact_mapping(response.body_json), ensure_ascii=False)
                if isinstance(response.body_json, (dict, list)) else response.body_text or "")
        if len(body) > self.config.capture_body_bytes:
            body = body[: self.config.capture_body_bytes] + "\n…（已截断）"
        return {
            "ok": response.ok,
            "status": response.status,
            "headers": redact_headers(response.headers),
            "body_text": body,
            "body_json": redact_mapping(response.body_json) if isinstance(response.body_json, (dict, list)) else response.body_json,
            "duration_ms": response.duration_ms,
            "url": redact_url(response.url),
            "error": response.error,
            "truncated": response.truncated,
            "size_bytes": response.size_bytes,
        }


def _unresolved_request_variables(case: CaseSpec, resolver: VariableResolver) -> List[str]:
    """收集请求模板里引用但当前解析不到的变量名（去重、保持出现顺序）。"""
    names: List[str] = []
    sources = [case.request.path, case.request.body]
    for item in list(case.request.query) + list(case.request.headers) + list(case.request.cookies):
        sources.append(item.value)
    for value in sources:
        if not isinstance(value, str):
            continue
        for match in VAR_PATTERN.finditer(value):
            name = match.group("name") if "name" in (match.groupdict() or {}) else None
            if name is None:
                continue
            if name not in names and resolver.get(name, None) is None:
                names.append(name)
    return names


class RunManager:
    """后台运行队列：提交、执行、取消、查询。"""

    def __init__(self, config: PlatformConfig, registry: ProjectRegistry, capacity: int = 32) -> None:
        self.config = config
        self.registry = registry
        self.capacity = max(4, int(capacity))
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue(maxsize=self.capacity)
        self._lock = threading.RLock()
        self._cancel_flags: Dict[str, threading.Event] = {}
        self._project_locks: Dict[str, threading.Lock] = {}
        self._threads: List[threading.Thread] = []
        self._payloads: Dict[str, Any] = {}
        self._running = False

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            for index in range(max(1, int(self.config.max_workers))):
                thread = threading.Thread(target=self._worker, name="hapitest-worker-%d" % index, daemon=True)
                thread.start()
                self._threads.append(thread)

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
            for _ in self._threads:
                try:
                    self._queue.put_nowait(None)
                except queue.Full:
                    pass
            threads, self._threads = self._threads, []
        for thread in threads:
            thread.join(timeout=timeout)

    def _worker(self) -> None:
        while True:
            try:
                run_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                with self._lock:
                    if not self._running:
                        return
                continue
            if run_id is None:
                return
            try:
                self._execute(run_id)
            except Exception:  # 后台线程绝不允许把异常抛出去
                self._record_fatal(run_id)
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------------ 提交
    def submit(self, request: RunRequest) -> RunResult:
        repository = self.registry.repository(request.project_id)
        project = self.registry.get(request.project_id)

        case_ids = list(request.case_ids)
        suite = None
        if request.suite_id is not None:
            suite = repository.get_suite(request.suite_id)
            if not case_ids:
                case_ids = list(suite.case_ids)
        if not case_ids:
            raise NotFoundError("没有可执行的用例：请提供 case_ids 或选择包含用例的套件")

        cases = repository.get_cases(case_ids)
        if not cases:
            raise NotFoundError("用例不存在：%s" % case_ids)
        case_ids = [int(case.id) for case in cases if case.id is not None]

        environment = repository.resolve_environment(request.env_id if request.env_id is not None else (suite.env_id if suite else None))
        run = RunResult(
            project_id=request.project_id,
            status="queued",
            trigger=request.trigger or "manual",
            suite_id=(suite.id if suite else None),
            suite_name=(suite.name if suite else ""),
            env_id=environment.id,
            env_name=environment.name,
            started_at=now_iso(),
            operator=request.operator,
            note=request.note,
            request_id=request.request_id,
            fingerprint=repository.fingerprint(case_ids),
        )
        run.logs.append("运行已排队：%d 条用例，环境=%s" % (len(case_ids), environment.name or "未配置"))
        repository.create_run(run)
        self._store_case_ids(run.id, case_ids, request.variables, project.id, request.signature())

        with self._lock:
            self._cancel_flags[run.id] = threading.Event()
        try:
            self._queue.put_nowait(run.id)
        except queue.Full:
            repository.set_run_status(run.id, "error")
            run.status = "error"
            run.logs.append("执行队列已满，运行未开始")
            repository.update_run(run)
            raise QueueFullError("执行队列已满（容量 %d），请稍后重试" % self.capacity)
        self.start()
        return run

    def cancel(self, project_id: str, run_id: str) -> bool:
        repository = self.registry.repository(project_id)
        run = repository.get_run(run_id, with_cases=False)
        if run.status in TERMINAL_STATUSES:
            return False
        with self._lock:
            flag = self._cancel_flags.get(run_id)
        if flag is not None:
            flag.set()
        repository.set_run_status(run_id, "canceled")
        run.status = "canceled"
        run.finished_at = now_iso()
        run.logs.append("运行已被取消")
        repository.update_run(run)
        return True

    # ------------------------------------------------------------------ 执行
    def _project_lock(self, project_id: str) -> threading.Lock:
        with self._lock:
            lock = self._project_locks.get(project_id)
            if lock is None:
                lock = threading.Lock()
                self._project_locks[project_id] = lock
            return lock

    def _execute(self, run_id: str) -> None:
        with self._lock:
            flag = self._cancel_flags.get(run_id)
        payload = self._load_payload(run_id)
        if payload is None:
            return
        project_id, case_ids, variables = payload
        repository = self.registry.repository(project_id)
        project = self.registry.get(project_id)
        lock = self._project_lock(project_id)

        # 同一项目串行执行，避免相互干扰；不同项目可并行
        with lock:
            run = repository.get_run(run_id, with_cases=False)
            if run.status == "canceled" or (flag is not None and flag.is_set()):
                run.logs.append("运行在开始前已被取消")
                run.status = "canceled"
                run.finished_at = now_iso()
                repository.update_run(run)
                return
            run.status = "running"
            run.logs.append("运行开始：%s" % now_iso())
            repository.update_run(run)

            environment = repository.resolve_environment(run.env_id)
            runner = CaseRunner(self.config, repository, project)
            started = time.perf_counter()
            cancel_check = (lambda: bool(flag and flag.is_set())) if flag else None

            suite = repository.get_suite(run.suite_id) if run.suite_id else None
            if suite is not None and suite.variables:
                # 套件级变量作为默认值：留空的项不覆盖环境变量，运行参数优先
                shared = {key: value for key, value in suite.variables.items() if value not in (None, "")}
                variables = dict(shared, **dict(variables))
            concurrency = max(1, int(suite.concurrency)) if suite else 1
            stop_on_failure = bool(suite and suite.failure_strategy == "stop")
            cases = repository.get_cases(case_ids)

            results: List[CaseResult] = []
            if concurrency > 1 and len(cases) > 1:
                # 并发执行时为每条用例创建独立执行器（独立 Cookie Jar），避免会话串扰
                with ThreadPoolExecutor(max_workers=min(concurrency, len(cases))) as pool:
                    futures = [
                        pool.submit(
                            CaseRunner(self.config, repository, project).run_case,
                            case,
                            environment,
                            dict(variables),
                            cancel_check,
                        )
                        for case in cases
                    ]
                    for future in futures:
                        try:
                            results.append(future.result())
                        except Exception as exc:  # 单个用例异常不影响整体
                            results.append(self._crash_result(exc))
            else:
                for case in cases:
                    if cancel_check is not None and cancel_check():
                        run.logs.append("运行被取消，剩余用例未执行")
                        break
                    try:
                        result = runner.run_case(case, environment, dict(variables), cancel_check)
                    except Exception as exc:
                        result = self._crash_result(exc, case)
                    results.append(result)
                    repository.save_case_result(run_id, result)
                    if stop_on_failure and result.status in ("failed", "error", "incomplete", "canceled"):
                        run.logs.append("套件配置为失败即停止，剩余用例未执行")
                        break

            if concurrency > 1:
                for result in results:
                    repository.save_case_result(run_id, result)

            run.case_results = results
            for result in results:
                # 运行级日志给出「跑了哪些用例、各自什么结果」的概览，细节在用例日志里
                run.logs.append(
                    "[%s] %s %s → %s（%dms%s）"
                    % (
                        result.started_at or now_iso(),
                        _status_mark(result.status),
                        result.case_name or ("用例 #%s" % result.case_id),
                        STATUS_LABELS.get(result.status, result.status),
                        result.duration_ms,
                        "，尝试 %d 次" % result.attempts if result.attempts > 1 else "",
                    )
                )
            run.recount()
            run.duration_ms = int((time.perf_counter() - started) * 1000)
            run.finished_at = now_iso()
            if flag is not None and flag.is_set() and run.status != "canceled":
                run.status = "canceled"
                run.logs.append("运行已被取消")
            else:
                run.finalize_status()
            run.logs.append("运行结束：%s，通过 %d/%d，耗时 %dms" % (run.status, run.passed, run.total, run.duration_ms))
            repository.update_run(run)
            try:
                vacuum_runs(repository.conn, self.config.run_retention)
                self._prune_payloads(project_id, repository)
            except Exception:
                pass
        with self._lock:
            self._cancel_flags.pop(run_id, None)
            self._payloads.pop(run_id, None)

    def _crash_result(self, exc: Exception, case: Optional[CaseSpec] = None) -> CaseResult:
        return CaseResult(
            case_id=(case.id if case else None),
            case_name=(case.name if case else "未知用例"),
            status="error",
            started_at=now_iso(),
            error="平台内部错误：%s" % exc,
            logs=[traceback.format_exc(limit=3)],
        )

    def _payload_path(self, project_id: str, run_id: str):
        return self.config.project_dir(project_id) / "runs" / ("%s.json" % run_id)

    def _store_case_ids(
        self,
        run_id: str,
        case_ids: Sequence[int],
        variables: Dict[str, Any],
        project_id: str,
        signature: str = "",
    ) -> None:
        """运行参数落盘：进程重启后仍能解释历史运行，也便于人工排查。"""
        import json

        path = self._payload_path(project_id, run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"run_id": run_id, "case_ids": [int(item) for item in case_ids], "variables": redact_mapping(variables or {}),
                   "signature": "sha256:" + hashlib.sha256(signature.encode("utf-8")).hexdigest() if signature else ""}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        with self._lock:
            # 执行使用内存中的真实参数；磁盘快照只保存脱敏值。
            self._payloads[run_id] = (project_id, list(payload["case_ids"]), dict(variables or {}))

    def _load_payload(self, run_id: str) -> Optional[Any]:
        import json

        with self._lock:
            cached = self._payloads.get(run_id)
        if cached is not None:
            return cached
        for record in self.registry.list_projects():
            path = self._payload_path(record.id, run_id)
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    return None
                return record.id, [int(item) for item in data.get("case_ids", [])], dict(data.get("variables") or {})
        return None

    def _prune_payloads(self, project_id: str, repository: Repository) -> int:
        """删除已被清理的运行对应的参数快照文件，避免数据目录无限增长。"""
        runs_dir = self.config.project_dir(project_id) / "runs"
        if not runs_dir.exists():
            return 0
        keep = {run.id for run in repository.list_runs(limit=max(1, self.config.run_retention))[0]}
        removed = 0
        for path in runs_dir.glob("*.json"):
            if path.stem not in keep:
                try:
                    path.unlink()
                    removed += 1
                    with self._lock:
                        self._payloads.pop(path.stem, None)
                except OSError:
                    pass
        return removed

    def _record_fatal(self, run_id: str) -> None:
        for record in self.registry.list_projects():
            try:
                repository = self.registry.repository(record.id)
                run = repository.get_run(run_id, with_cases=False)
            except NotFoundError:
                continue
            except Exception:
                return
            if run.status in TERMINAL_STATUSES:
                return
            run.status = "error"
            run.finished_at = now_iso()
            run.logs.append("执行器异常终止：\n%s" % traceback.format_exc(limit=5))
            try:
                repository.update_run(run)
            except Exception:
                pass
            return

    # ------------------------------------------------------------------ 幂等与等待
    def find_existing(self, request: RunRequest) -> Optional[RunResult]:
        """request_id 幂等：同参数返回既有运行，不同参数抛冲突。"""
        if not request.request_id:
            return None
        try:
            repository = self.registry.repository(request.project_id)
        except NotFoundError:
            return None
        existing = repository.find_run_by_request_id(request.request_id)
        if existing is None:
            return None
        stored = self._stored_signature(request.project_id, existing.id)
        signature = request.signature()
        # 兼容已有运行保存的原始签名，新运行只保存摘要。
        if stored and stored not in (signature, "sha256:" + hashlib.sha256(signature.encode("utf-8")).hexdigest()):
            from .errors import ConflictError

            raise ConflictError(
                "request_id %s 已用于其它参数的运行（%s），请更换 request_id" % (request.request_id, existing.id)
            )
        return existing

    def _stored_signature(self, project_id: str, run_id: str) -> str:
        import json

        path = self._payload_path(project_id, run_id)
        if not path.exists():
            return ""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return ""
        return str(data.get("signature", "") or "")

    def wait(self, project_id: str, run_id: str, timeout_s: float = 600.0) -> RunResult:
        """阻塞等待运行进入终态。"""
        repository = self.registry.repository(project_id)
        deadline = time.time() + max(1.0, float(timeout_s))
        while time.time() < deadline:
            current = repository.get_run(run_id, with_cases=True)
            if current.status in TERMINAL_STATUSES:
                return current
            time.sleep(0.2)
        return repository.get_run(run_id, with_cases=True)

    # ------------------------------------------------------------------ 同步执行
    def run_sync(self, request: RunRequest, timeout_s: float = 900.0) -> RunResult:
        """同步执行（CLI 与单条调试使用）。"""
        run = self.submit(request)
        return self.wait(request.project_id, run.id, timeout_s=timeout_s)
