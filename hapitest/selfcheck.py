"""端到端自检：用本地 mock 服务证明平台真的能跑通。

自检不访问外网、不依赖数据库、不污染真实数据目录（默认使用临时目录）。
每一步都产出可核对的结果，最终汇总为 JSON 报告，可直接作为交付证据。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import PLATFORM_NAME, __version__
from .config import PlatformConfig
from .models import CaseSpec, EnvironmentSpec, RequestSpec, SuiteSpec, now_iso
from .report import to_junit_xml, to_html_report, to_json_report
from .repository import ProjectRegistry
from .runner import RunManager, RunRequest
from .sync import apply_import, load_import


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_port(port: int, timeout_s: float = 15.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return False


class _Checks:
    def __init__(self) -> None:
        self.items: List[Dict[str, Any]] = []

    def add(self, name: str, passed: bool, detail: str = "", evidence: Any = None) -> bool:
        self.items.append(
            {
                "name": name,
                "status": "passed" if passed else "failed",
                "detail": detail,
                "evidence": evidence,
            }
        )
        return passed

    @property
    def failed(self) -> List[Dict[str, Any]]:
        return [item for item in self.items if item["status"] == "failed"]


def run_selfcheck(config: PlatformConfig, keep: bool = False) -> Dict[str, Any]:
    checks = _Checks()
    temp_dir: Optional[tempfile.TemporaryDirectory] = None
    process: Optional[subprocess.Popen] = None
    started = time.time()
    summary: Dict[str, Any] = {}

    try:
        checks.add("platform.importable", True, "%s %s" % (PLATFORM_NAME, __version__))

        if keep:
            data_dir = config.data_dir / "selfcheck"
            data_dir.mkdir(parents=True, exist_ok=True)
        else:
            temp_dir = tempfile.TemporaryDirectory(prefix="hapitest-selfcheck-")
            data_dir = Path(temp_dir.name)
        check_config = PlatformConfig(root_dir=config.root_dir, data_dir=data_dir)
        check_config.ensure_dirs()

        # 1. 启动 mock 服务
        mock_script = config.root_dir / "examples" / "mock_service.py"
        if not checks.add("mock.script_exists", mock_script.exists(), str(mock_script)):
            return _report(checks, summary, started)
        port = _free_port()
        process = subprocess.Popen(
            [sys.executable, str(mock_script), "--port", str(port)],
            cwd=str(config.root_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        ready = _wait_port(port)
        checks.add("mock.started", ready, "http://127.0.0.1:%d" % port)
        if not ready:
            output = b""
            if process.poll() is not None and process.stdout is not None:
                output = process.stdout.read()[:2000]
            checks.add("mock.output", False, output.decode("utf-8", "replace"))
            return _report(checks, summary, started)
        base_url = "http://127.0.0.1:%d" % port

        # 2. 建项目 + 导入示例规格
        registry = ProjectRegistry(check_config)
        record = registry.create(name="自检项目", project_id="selfcheck", description="端到端自检")
        repository = registry.repository(record.id)
        spec_path = config.root_dir / "examples" / "demo_specs" / "demo_openapi.json"
        checks.add("spec.exists", spec_path.exists(), str(spec_path))
        result = load_import(check_config, path=str(spec_path), options={"base_url": base_url, "generate_cases": True})
        checks.add(
            "import.parsed",
            len(result.endpoints) >= 5 and len(result.cases) >= 5,
            "接口 %d 个，用例 %d 条" % (len(result.endpoints), len(result.cases)),
            evidence=result.stats.to_dict(),
        )
        stats = apply_import(repository, result, mode="merge")
        checks.add("import.persisted", stats.created >= 5, "新增接口 %d，新增用例 %d" % (stats.created, stats.cases_created))

        # 幂等：再次导入不应产生新接口
        result_again = load_import(check_config, path=str(spec_path), options={"base_url": base_url, "generate_cases": True})
        stats_again = apply_import(repository, result_again, mode="merge")
        checks.add(
            "import.idempotent",
            stats_again.created == 0 and stats_again.updated == 0 and stats_again.cases_created == 0,
            "二次导入：新增 %d，更新 %d，未变化 %d，新增用例 %d"
            % (stats_again.created, stats_again.updated, stats_again.unchanged, stats_again.cases_created),
        )

        # 3. 环境 + 套件
        environment = repository.default_environment()
        if environment is None:
            environment = repository.create_environment(
                EnvironmentSpec(name="自检环境", base_url=base_url, is_default=True, created_at=now_iso())
            )
        else:
            environment.base_url = base_url
            repository.update_environment(environment.id, environment)
        cases, _ = repository.list_cases(limit=1000)
        smoke_ids = [int(case.id) for case in cases if case.source == "openapi" and case.id is not None]
        suite = repository.create_suite(
            SuiteSpec(name="自检套件", description="导入生成的冒烟用例", case_ids=smoke_ids, env_id=environment.id)
        )
        checks.add("suite.created", bool(smoke_ids), "套件包含 %d 条用例" % len(smoke_ids))

        manager = RunManager(check_config, registry)
        manager.start()
        try:
            # 4. 正向：全部通过
            run = manager.run_sync(RunRequest(project_id=record.id, suite_id=suite.id, env_id=environment.id, trigger="api"))
            checks.add(
                "run.smoke_passed",
                run.status == "passed" and run.passed == len(smoke_ids),
                "状态=%s，通过 %d/%d，失败 %d，错误 %d，未完成 %d"
                % (run.status, run.passed, run.total, run.failed, run.errored, run.incomplete),
                evidence=run.to_dict(with_cases=False),
            )
            summary["smoke_run"] = run.id
            summary["smoke_total"] = run.total

            # 5. 反向：断言失败必须被判为 failed（而不是通过）
            from .models import AssertionSpec

            failing = repository.create_case(
                CaseSpec(
                    name="自检 · 断言失败用例",
                    folder="自检",
                    request=RequestSpec(method="GET", path="/api/health"),
                    assertions=[AssertionSpec(type="status_code", operator="eq", expected=599)],
                )
            )
            fail_run = manager.run_sync(
                RunRequest(project_id=record.id, case_ids=[failing.id], env_id=environment.id, trigger="api")
            )
            checks.add(
                "run.assertion_failure_detected",
                fail_run.status == "failed",
                "状态=%s（期望 failed）" % fail_run.status,
                evidence=[item.to_dict() for item in fail_run.case_results],
            )

            # 6. 缺条件：没有 base_url 必须 incomplete
            orphan = repository.create_case(
                CaseSpec(
                    name="自检 · 未配置地址",
                    folder="自检",
                    request=RequestSpec(method="GET", path="/api/health", base_url=""),
                    assertions=[AssertionSpec(type="status_code", operator="eq", expected=200)],
                )
            )
            no_env = EnvironmentSpec(name="空环境", base_url="", is_default=False, created_at=now_iso())
            saved_env = repository.create_environment(no_env)
            orphan_run = manager.run_sync(
                RunRequest(project_id=record.id, case_ids=[orphan.id], env_id=saved_env.id, trigger="api")
            )
            checks.add(
                "run.missing_base_url_incomplete",
                orphan_run.status == "incomplete",
                "状态=%s（期望 incomplete）" % orphan_run.status,
            )

            # 7. 请求不可达必须 error
            unreachable = repository.create_case(
                CaseSpec(
                    name="自检 · 地址不可达",
                    folder="自检",
                    request=RequestSpec(method="GET", path="/api/health", base_url="http://127.0.0.1:1", timeout_ms=2000),
                    assertions=[AssertionSpec(type="status_code", operator="eq", expected=200)],
                )
            )
            error_run = manager.run_sync(
                RunRequest(project_id=record.id, case_ids=[unreachable.id], trigger="api")
            )
            checks.add(
                "run.unreachable_error",
                error_run.status == "error",
                "状态=%s（期望 error）" % error_run.status,
            )

            # 8. 报告可用性
            xml_text = to_junit_xml(run)
            html_text = to_html_report(run, project_name=record.name)
            json_text = to_json_report(run)
            try:
                root = ET.fromstring(xml_text)
                cases_in_xml = len(root.findall(".//testcase"))
                xml_ok = cases_in_xml == run.total and root.tag == "testsuites"
            except ET.ParseError as exc:
                xml_ok = False
                cases_in_xml = -1
                checks.add("report.junit_parse_error", False, str(exc))
            checks.add("report.junit", xml_ok, "XML 中 testcase %d 条，运行用例 %d 条" % (cases_in_xml, run.total))
            checks.add("report.html", "<html" in html_text.lower() and run.id in html_text, "%d 字节" % len(html_text))
            checks.add("report.json", run.id in json_text, "%d 字节" % len(json_text))

            # 9. 隔离：另一个项目看不到本项目数据
            other = registry.create(name="隔离检查项目", project_id="selfcheck-isolated")
            other_repo = registry.repository(other.id)
            other_counts = other_repo.counts()
            checks.add(
                "isolation.project_scoped",
                other_counts.get("endpoint", 0) == 0 and other_counts.get("test_case", 0) == 0,
                "另一项目接口 %d，用例 %d" % (other_counts.get("endpoint", 0), other_counts.get("test_case", 0)),
            )
            db_paths = {
                str(check_config.project_db_path(record.id).resolve()),
                str(check_config.project_db_path(other.id).resolve()),
            }
            checks.add("isolation.separate_db", len(db_paths) == 2, "、".join(sorted(db_paths)))
            try:
                registry.get("../../etc")
                traversal_blocked = False
            except Exception:
                traversal_blocked = True
            checks.add("isolation.path_traversal_blocked", traversal_blocked, "非法项目 ID 被拒绝")
        finally:
            manager.stop()

        # 10. 历史与对比
        runs, total_runs = repository.list_runs(limit=20)
        checks.add("history.recorded", total_runs >= 4, "共 %d 次运行" % total_runs)
        summary["runs"] = [item.id for item in runs]
    except Exception as exc:  # 自检自身异常也要变成报告
        import traceback

        checks.add("selfcheck.exception", False, "%s: %s" % (type(exc).__name__, exc), evidence=traceback.format_exc())
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        if temp_dir is not None:
            temp_dir.cleanup()

    return _report(checks, summary, started)


def _report(checks: _Checks, summary: Dict[str, Any], started: float) -> Dict[str, Any]:
    failed = checks.failed
    return {
        "status": "passed" if not failed else "failed",
        "platform": "%s %s" % (PLATFORM_NAME, __version__),
        "finished_at": now_iso(),
        "duration_ms": int((time.time() - started) * 1000),
        "total": len(checks.items),
        "passed": len(checks.items) - len(failed),
        "failed": len(failed),
        "checks": checks.items,
        "summary": summary,
        "environment": {"python": sys.version.split()[0], "cwd": os.getcwd(), "pid": os.getpid()},
    }
