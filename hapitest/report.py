"""运行报告导出：JUnit XML、JSON、自包含 HTML 与一行中文摘要（INTERFACES.md 1.6）。

设计要点：

- **JUnit XML** 面向 Jenkins / GitLab：``<testsuite>`` 必须带
  ``name/tests/failures/errors/skipped/time/timestamp/hostname``；``timestamp`` 去掉时区后缀
  （JUnit XSD 的 ``dateTime`` 不接受结尾的 ``Z``）；每条用例同时给 ``classname`` 与 ``name``，
  否则 Jenkins 会把没有 ``classname`` 的行折叠到一起。
- **状态映射**：``failed`` → ``<failure type="assertion">``；``error`` → ``<error type="request">``；
  ``incomplete`` → ``<error type="incomplete">``（**故意不用 ``<skipped>``**：未完成表示「缺条件没跑成」，
  CI 必须把它当失败而不是通过）；``skipped`` → ``<skipped/>``。因此 ``errors = errored + incomplete``。
- **HTML** 单文件自包含：内联 CSS、零 JS、零外部资源（无 CDN/字体/图片），
  浅色/深色自适应，``html.escape`` 所有动态内容，并带简单打印样式（可直接打印成 PDF）。
- 所有文本经过 XML 非法控制字符过滤与长度截断，避免报告文件被脏数据破坏。
"""

from __future__ import annotations

import html
import json
import re
import socket
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .models import CaseResult, RunResult

__all__ = [
    "STATUS_LABELS",
    "TRIGGER_LABELS",
    "to_html_report",
    "to_json_report",
    "to_junit_xml",
    "summarize_text",
    "write_reports",
]

#: 用例状态 → 中文文案（报告与摘要共用）
STATUS_LABELS: Dict[str, str] = {
    "passed": "通过",
    "failed": "失败",
    "error": "错误",
    "incomplete": "未完成",
    "skipped": "跳过",
}

#: 运行触发方式 → 中文文案
TRIGGER_LABELS: Dict[str, str] = {
    "manual": "手动",
    "cli": "命令行",
    "api": "接口调用",
    "schedule": "定时任务",
    "ci": "CI",
}

#: XML 1.0 允许的字符：制表/换行/回车 + >=0x20（排除代理区与非法码位）
_ILLEGAL_XML_RE = re.compile("[^\u0009\u000a\u000d\u0020-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9_-]+")

#: 单条用例 system-out / system-err 的字符上限，避免报告畸形膨胀
SYSTEM_OUT_LIMIT = 4000
SYSTEM_ERR_LIMIT = 4000
#: system-out 中响应体保留的字符数
SYSTEM_BODY_LIMIT = 2000
#: HTML 中响应体保留的字符数
HTML_BODY_LIMIT = 4000


# --------------------------------------------------------------------------- #
# 公共入口
# --------------------------------------------------------------------------- #
def to_junit_xml(run: RunResult) -> str:
    """导出 JUnit XML（Jenkins / GitLab / GitLab CI 可直接解析）。"""
    cases = list(run.case_results or [])
    counts = _counts(run, cases)
    errors = counts["errored"] + counts["incomplete"]
    run_time = _run_seconds(run, cases)

    root = ET.Element(
        "testsuites",
        {
            "name": run.project_id or "HapiTest",
            "tests": str(counts["total"]),
            "failures": str(counts["failed"]),
            "errors": str(errors),
            "skipped": str(counts["skipped"]),
            "time": run_time,
        },
    )

    suite_name = "%s · %s" % (run.suite_name or "项目", run.env_name or "默认环境")
    suite = ET.SubElement(
        root,
        "testsuite",
        {
            "name": suite_name,
            "tests": str(counts["total"]),
            "failures": str(counts["failed"]),
            # incomplete 也计入 errors：CI 不能把「没跑成」当通过
            "errors": str(errors),
            "skipped": str(counts["skipped"]),
            "time": run_time,
            # JUnit XSD 的 dateTime 不接受结尾 Z / 微秒 / 时区偏移，这里只保留秒精度本地时刻文本
            "timestamp": _junit_timestamp(run.started_at),
            "hostname": _hostname(),
        },
    )
    suite.append(_properties(run, counts))

    for index, case in enumerate(cases, start=1):
        suite.append(_testcase(case, index, suite_name))

    _indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


def to_json_report(run: RunResult) -> str:
    """导出 JSON（直接使用模型的 ``to_dict()``，中文不转义）。"""
    try:
        return json.dumps(run.to_dict(), ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        # to_dict 中出现不可序列化对象时退化为字符串，保证报告一定能导出
        return json.dumps(run.to_dict(), ensure_ascii=False, indent=2, default=str)


def to_html_report(run: RunResult, project_name: str = "", title: str = "") -> str:
    """导出自包含 HTML 报告（内联 CSS、零 JS、零外部资源，可直接打印成 PDF）。"""
    cases = list(run.case_results or [])
    counts = _counts(run, cases)
    status = run.status or "incomplete"
    page_title = title or "%s 测试报告" % (project_name or run.suite_name or "HapiTest")
    trigger = TRIGGER_LABELS.get(run.trigger, run.trigger or "-")

    cards: List[Tuple[str, str, str]] = [
        ("总数", str(counts["total"]), "total"),
        ("通过", str(counts["passed"]), "passed"),
        ("失败", str(counts["failed"]), "failed"),
        ("错误", str(counts["errored"]), "error"),
        ("未完成", str(counts["incomplete"]), "incomplete"),
        ("跳过", str(counts["skipped"]), "skipped"),
        ("耗时", _format_seconds(run.duration_ms), "meta"),
        ("触发方式", trigger, "meta"),
        ("环境", run.env_name or "-", "meta"),
        ("开始时间", run.started_at or "-", "meta"),
        ("运行 ID", run.id or "-", "meta"),
    ]
    cards_html = "\n".join(
        '      <div class="card card-%s"><span class="card-k">%s</span>'
        '<span class="card-v">%s</span></div>' % (_esc(kind), _esc(label), _esc(value))
        for label, value, kind in cards
    )

    rows: List[str] = []
    for index, case in enumerate(cases, start=1):
        rows.append(
            "        <tr class=\"row-%s\"><td>%d</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (
                _esc(_safe_status(case.status)),
                index,
                _esc(case.case_name or _fallback_case_name(case)),
                _esc(_status_label(case.status)),
                _esc(_format_seconds(case.duration_ms)),
                _esc(_assertion_brief(case)),
            )
        )
    overview = "\n".join(rows) if rows else '        <tr><td colspan="5">没有用例记录</td></tr>'

    details = "\n".join(_case_details(case, index) for index, case in enumerate(cases, start=1))
    if not details:
        details = "    <p>没有用例记录。</p>"

    logs_html = ""
    if run.logs:
        logs_html = (
            '    <h2>运行日志</h2>\n    <pre class="block">%s</pre>\n'
            % _esc("\n".join(str(item) for item in run.logs))
        )

    return _HTML_TEMPLATE % {
        "lang": "zh-CN",
        "title": _esc(page_title),
        "heading": _esc(page_title),
        "subtitle": _esc(
            "项目：%s · 套件：%s · 状态：%s · 生成时间：%s"
            % (
                project_name or run.project_id or "-",
                run.suite_name or "-",
                _status_label(status),
                _now_text(),
            )
        ),
        "status_class": _esc(_safe_status(status)),
        "cards": cards_html,
        "overview": overview,
        "details": details,
        "logs": logs_html,
    }


def write_reports(run: RunResult, directory: Path, project_name: str = "") -> Dict[str, str]:
    """把 XML / HTML / JSON 写入 ``<directory>/<project_name or "reports">/``。

    返回 ``{"xml": 路径, "html": 路径, "json": 路径}``（字符串路径）。
    """
    base = Path(directory).expanduser()
    folder = base / (str(project_name or "").strip() or "reports")
    folder.mkdir(parents=True, exist_ok=True)

    run_id = _safe_filename(run.id or "run")
    paths = {
        "xml": folder / ("run-%s.xml" % run_id),
        "html": folder / ("run-%s.html" % run_id),
        "json": folder / ("run-%s.json" % run_id),
    }
    paths["xml"].write_text(to_junit_xml(run), encoding="utf-8")
    paths["html"].write_text(to_html_report(run, project_name=project_name), encoding="utf-8")
    paths["json"].write_text(to_json_report(run), encoding="utf-8")
    return {key: str(value) for key, value in paths.items()}


def summarize_text(run: RunResult) -> str:
    """一行中文摘要，例如 ``运行 run_ab12 通过 8/10，失败 1，错误 1，未完成 0，耗时 2.3s → failed``。"""
    counts = _counts(run, list(run.case_results or []))
    text = "运行 %s 通过 %d/%d" % (run.id or "(未命名)", counts["passed"], counts["total"])
    text += "，失败 %d，错误 %d，未完成 %d" % (counts["failed"], counts["errored"], counts["incomplete"])
    if counts["skipped"]:
        text += "，跳过 %d" % counts["skipped"]
    text += "，耗时 %ss → %s" % (_short_seconds(run.duration_ms), run.status or "incomplete")
    return text


# --------------------------------------------------------------------------- #
# 计数与文案
# --------------------------------------------------------------------------- #
def _counts(run: RunResult, cases: List[CaseResult]) -> Dict[str, int]:
    """统计口径：有用例明细时以明细为准，否则回退到运行汇总字段。"""
    if cases:
        return {
            "total": len(cases),
            "passed": sum(1 for item in cases if item.status == "passed"),
            "failed": sum(1 for item in cases if item.status == "failed"),
            "errored": sum(1 for item in cases if item.status == "error"),
            "incomplete": sum(1 for item in cases if item.status == "incomplete"),
            "skipped": sum(1 for item in cases if item.status == "skipped"),
        }
    return {
        "total": int(run.total or 0),
        "passed": int(run.passed or 0),
        "failed": int(run.failed or 0),
        "errored": int(run.errored or 0),
        "incomplete": int(run.incomplete or 0),
        "skipped": int(run.skipped or 0),
    }


def _safe_status(status: str) -> str:
    text = str(status or "").strip().lower()
    return text if text in ("passed", "failed", "error", "incomplete", "skipped", "running", "queued") else "incomplete"


def _status_label(status: str) -> str:
    text = str(status or "").strip().lower()
    return STATUS_LABELS.get(text, text or "-")


def _run_seconds(run: RunResult, cases: List[CaseResult]) -> str:
    duration = int(run.duration_ms or 0)
    if duration <= 0 and cases:
        duration = sum(int(item.duration_ms or 0) for item in cases)
    return _seconds(duration)


def _seconds(duration_ms: Any) -> str:
    """毫秒 → 秒（3 位小数），JUnit 的 time 属性统一用它。"""
    try:
        value = int(duration_ms or 0)
    except (TypeError, ValueError):
        value = 0
    return "%.3f" % (max(value, 0) / 1000.0)


def _format_seconds(duration_ms: Any) -> str:
    return "%ss" % _seconds(duration_ms)


def _short_seconds(duration_ms: Any) -> str:
    """摘要里用的短耗时：保留 1 位小数。"""
    try:
        value = int(duration_ms or 0)
    except (TypeError, ValueError):
        value = 0
    return "%.1f" % (max(value, 0) / 1000.0)


def _junit_timestamp(value: Any) -> str:
    """把 ``2026-01-01T00:00:00Z`` 转成 JUnit 接受的 ``2026-01-01T00:00:00``。"""
    text = str(value or "").strip()
    match = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", text)
    if match:
        return match.group(1)
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _hostname() -> str:
    try:
        return socket.gethostname() or "localhost"
    except Exception:  # pragma: no cover - 极端环境兜底
        return "localhost"


def _fallback_case_name(case: CaseResult) -> str:
    if case.case_id is not None:
        return "用例 #%s" % case.case_id
    return "未命名用例"


def _case_folder(case: CaseResult) -> str:
    """用例分组：优先取用例预览里的 folder/group，缺失时归入「未分组」。"""
    request = case.request if isinstance(case.request, dict) else {}
    response = case.response if isinstance(case.response, dict) else {}
    for container in (request, response):
        for key in ("folder", "case_folder", "group"):
            value = container.get(key)
            if value:
                return str(value)
    return "未分组"


def _assertion_brief(case: CaseResult) -> str:
    items = list(case.assertions or [])
    if not items:
        return "-"
    passed = sum(1 for item in items if item.passed)
    return "%d/%d" % (passed, len(items))


def _format_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


# --------------------------------------------------------------------------- #
# JUnit XML 细节
# --------------------------------------------------------------------------- #
def _clean_text(value: Any, limit: int = 0) -> str:
    """过滤 XML 非法控制字符并按需截断。"""
    text = "" if value is None else str(value)
    text = _ILLEGAL_XML_RE.sub("", text)
    if limit > 0 and len(text) > limit:
        text = text[:limit] + "…（已截断）"
    return text


def _properties(run: RunResult, counts: Dict[str, int]) -> ET.Element:
    properties = ET.Element("properties")
    entries = [
        ("platform", "HapiTest"),
        ("platform_version", _platform_version()),
        ("project", run.project_id or ""),
        ("run_id", run.id or ""),
        ("suite", run.suite_name or ""),
        ("env", run.env_name or ""),
        ("trigger", run.trigger or ""),
        ("status", run.status or ""),
        ("total", str(counts["total"])),
        ("passed", str(counts["passed"])),
        ("failed", str(counts["failed"])),
        ("errored", str(counts["errored"])),
        ("incomplete", str(counts["incomplete"])),
        ("skipped", str(counts["skipped"])),
        ("started_at", run.started_at or ""),
        ("finished_at", run.finished_at or ""),
    ]
    if run.fingerprint:
        entries.append(("fingerprint", run.fingerprint))
    for name, value in entries:
        if value == "" and name not in ("project", "run_id", "status", "platform"):
            continue
        ET.SubElement(properties, "property", {"name": name, "value": _clean_text(value)})
    return properties


def _platform_version() -> str:
    try:
        from . import __version__

        return str(__version__)
    except Exception:  # pragma: no cover - 防御式兜底
        return "0.1.0"


def _testcase(case: CaseResult, index: int, suite_name: str) -> ET.Element:
    name = case.case_name or _fallback_case_name(case)
    classname = "%s.%s" % (suite_name, _case_folder(case))
    node = ET.Element(
        "testcase",
        {
            "classname": _clean_text(classname),
            "name": _clean_text(name),
            "time": _seconds(case.duration_ms),
        },
    )

    status = str(case.status or "").strip().lower()
    if status == "failed":
        failed = [item for item in (case.assertions or []) if not item.passed]
        message = ""
        for item in failed:
            if item.message:
                message = item.message
                break
        if not message:
            message = "%d 条断言失败" % len(failed) if failed else "断言失败"
        failure = ET.SubElement(
            node,
            "failure",
            {"message": _clean_text(message, 500), "type": "assertion"},
        )
        failure.text = _clean_text(_failure_detail(case), 4000) or None
    elif status == "error":
        error = ET.SubElement(
            node,
            "error",
            {"message": _clean_text(case.error or "请求失败", 500), "type": "request"},
        )
        error.text = _clean_text(_error_detail(case), 4000) or None
    elif status == "incomplete":
        # 未完成 = 缺少执行条件/未执行：映射为 <error> 而不是 <skipped>，
        # 这样 Jenkins/GitLab 会把它算作失败，符合 models.EXIT_CODES 的「缺条件不算通过」原则。
        error = ET.SubElement(
            node,
            "error",
            {"message": _clean_text(case.error or "缺少执行条件或未执行", 500), "type": "incomplete"},
        )
        error.text = _clean_text(_error_detail(case) or case.error, 4000) or None
    elif status == "skipped":
        ET.SubElement(node, "skipped", {"message": _clean_text(case.error or "已跳过", 500)})

    system_out = ET.SubElement(node, "system-out")
    system_out.text = _clean_text(_system_out(case), SYSTEM_OUT_LIMIT) or "（无请求记录）"
    system_err = ET.SubElement(node, "system-err")
    system_err.text = _clean_text(_system_err(case), SYSTEM_ERR_LIMIT) or "（无错误日志）"
    return node


def _failure_detail(case: CaseResult) -> str:
    lines: List[str] = []
    for index, item in enumerate(case.assertions or [], start=1):
        if item.passed:
            continue
        describe = item.describe or item.target or item.type or "断言"
        line = "#%d %s: 期望 %s，实际 %s" % (
            index,
            describe,
            _format_value(item.expected),
            _format_value(item.actual),
        )
        if item.message:
            line += "（%s）" % item.message
        lines.append(line)
    if case.error:
        lines.append("[错误] %s" % case.error)
    return "\n".join(lines)


def _error_detail(case: CaseResult) -> str:
    parts: List[str] = []
    response_error = ""
    if isinstance(case.response, dict):
        response_error = str(case.response.get("error") or "")
    if case.error:
        parts.append(case.error)
    elif response_error:
        parts.append(response_error)
    parts.extend(str(item) for item in (case.logs or []))
    return "\n".join(parts)


def _system_out(case: CaseResult) -> str:
    request = case.request if isinstance(case.request, dict) else {}
    response = case.response if isinstance(case.response, dict) else {}
    method = str(request.get("method") or response.get("method") or "GET").upper()
    url = str(request.get("url") or response.get("url") or "")
    lines = ["[请求] %s %s" % (method, url or "-")]

    headers = request.get("headers") or {}
    if isinstance(headers, dict) and headers:
        lines.append("[请求头] %s" % "; ".join("%s: %s" % (key, value) for key, value in headers.items()))
    body = request.get("body")
    if body:
        lines.append("[请求体] %s" % _format_value(body)[:500])

    status = response.get("status")
    lines.append(
        "[响应] 状态 %s · 耗时 %sms · 大小 %s 字节 · 截断 %s"
        % (
            "无" if status is None else status,
            int(response.get("duration_ms") or case.duration_ms or 0),
            int(response.get("size_bytes") or 0),
            "是" if response.get("truncated") else "否",
        )
    )
    response_headers = response.get("headers") or {}
    if isinstance(response_headers, dict) and response_headers:
        lines.append(
            "[响应头] %s" % "; ".join("%s: %s" % (key, value) for key, value in response_headers.items())
        )
    body_text = str(response.get("body_text") or "")
    if body_text:
        lines.append("[响应体]\n%s" % body_text[:SYSTEM_BODY_LIMIT])
    if not response:
        lines.append("[响应] 无响应记录")
    return "\n".join(lines)


def _system_err(case: CaseResult) -> str:
    parts: List[str] = []
    if case.error:
        parts.append("[错误] %s" % case.error)
    response_error = ""
    if isinstance(case.response, dict):
        response_error = str(case.response.get("error") or "")
    if response_error and response_error != case.error:
        parts.append("[响应错误] %s" % response_error)
    parts.extend("[日志] %s" % item for item in (case.logs or []))
    return "\n".join(parts) if parts else "（无）"


def _indent(root: ET.Element) -> None:
    try:
        ET.indent(root, space="  ")  # Python 3.9+
    except AttributeError:  # pragma: no cover - 兼容极旧解释器
        pass


# --------------------------------------------------------------------------- #
# HTML 细节
# --------------------------------------------------------------------------- #
def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _case_details(case: CaseResult, index: int) -> str:
    status = _safe_status(case.status)
    open_attr = " open" if status != "passed" else ""
    name = case.case_name or _fallback_case_name(case)
    request = case.request if isinstance(case.request, dict) else {}
    response = case.response if isinstance(case.response, dict) else {}

    blocks: List[str] = []
    blocks.append("      <h3>请求</h3>")
    blocks.append("      <table class=\"kv\">")
    blocks.append(
        "        <tr><th>方法</th><td><code>%s</code></td></tr>"
        % _esc(str(request.get("method") or response.get("method") or "-").upper())
    )
    blocks.append(
        "        <tr><th>地址</th><td><code>%s</code></td></tr>"
        % _esc(request.get("url") or response.get("url") or "-")
    )
    blocks.append(
        "        <tr><th>请求体类型</th><td>%s</td></tr>" % _esc(request.get("body_type") or "-")
    )
    blocks.append("      </table>")

    headers = request.get("headers") or {}
    if isinstance(headers, dict) and headers:
        blocks.append("      <h4>请求头</h4>")
        blocks.append("      <pre class=\"block\">%s</pre>" % _esc(_mapping_text(headers)))
    body = request.get("body")
    if body:
        blocks.append("      <h4>请求体</h4>")
        blocks.append("      <pre class=\"block\">%s</pre>" % _esc(_format_value(body)))

    blocks.append("      <h3>响应</h3>")
    blocks.append("      <table class=\"kv\">")
    blocks.append(
        "        <tr><th>状态码</th><td>%s</td></tr>"
        % _esc("无" if response.get("status") is None else response.get("status"))
    )
    blocks.append("        <tr><th>耗时</th><td>%s</td></tr>" % _esc(_format_seconds(case.duration_ms)))
    blocks.append(
        "        <tr><th>大小</th><td>%s 字节%s</td></tr>"
        % (
            _esc(int(response.get("size_bytes") or 0)),
            "（已截断）" if response.get("truncated") else "",
        )
    )
    blocks.append("      </table>")

    response_headers = response.get("headers") or {}
    if isinstance(response_headers, dict) and response_headers:
        blocks.append("      <h4>响应头</h4>")
        blocks.append("      <pre class=\"block\">%s</pre>" % _esc(_mapping_text(response_headers)))
    body_text = str(response.get("body_text") or "")
    if body_text:
        shown = body_text[:HTML_BODY_LIMIT]
        suffix = "…（已截断展示）" if len(body_text) > HTML_BODY_LIMIT else ""
        blocks.append("      <h4>响应体</h4>")
        blocks.append("      <pre class=\"block\">%s%s</pre>" % (_esc(shown), _esc(suffix)))

    blocks.append("      <h3>断言</h3>")
    assertions = list(case.assertions or [])
    if assertions:
        rows = []
        for position, item in enumerate(assertions, start=1):
            rows.append(
                "          <tr class=\"%s\"><td>%d</td><td>%s</td><td>%s</td><td>%s</td>"
                "<td>%s</td><td>%s</td></tr>"
                % (
                    "ok" if item.passed else "bad",
                    position,
                    _esc(item.describe or item.target or item.type or "-"),
                    _esc(_format_value(item.expected)),
                    _esc(_format_value(item.actual)),
                    _esc("通过" if item.passed else "失败"),
                    _esc(item.message or ""),
                )
            )
        blocks.append("      <table class=\"assertions\">")
        blocks.append(
            "        <thead><tr><th>#</th><th>断言</th><th>期望</th><th>实际</th><th>结果</th><th>说明</th></tr></thead>"
        )
        blocks.append("        <tbody>")
        blocks.extend(rows)
        blocks.append("        </tbody>")
        blocks.append("      </table>")
    else:
        blocks.append("      <p class=\"muted\">没有断言。</p>")

    extracted = case.extracted if isinstance(case.extracted, dict) else {}
    if extracted:
        blocks.append("      <h3>提取变量</h3>")
        blocks.append("      <table class=\"kv\">")
        for key, value in extracted.items():
            blocks.append(
                "        <tr><th>%s</th><td><code>%s</code></td></tr>" % (_esc(key), _esc(_format_value(value)))
            )
        blocks.append("      </table>")

    if case.error or response.get("error"):
        blocks.append("      <h3>错误</h3>")
        blocks.append("      <pre class=\"block error\">%s</pre>" % _esc(case.error or response.get("error")))

    if case.logs:
        blocks.append("      <h3>日志</h3>")
        blocks.append(
            "      <pre class=\"block\">%s</pre>" % _esc("\n".join(str(item) for item in case.logs))
        )

    if case.steps:
        blocks.append("      <h3>步骤</h3>")
        blocks.append("      <pre class=\"block\">%s</pre>" % _esc(_json_text(case.steps)))

    return (
        '    <details class="case status-%s"%s>\n'
        '      <summary><span class="badge badge-%s">%s</span>'
        '<span class="case-name">#%d %s</span>'
        '<span class="case-time">%s</span></summary>\n'
        '      <div class="case-body">\n%s\n      </div>\n    </details>'
        % (
            _esc(status),
            open_attr,
            _esc(status),
            _esc(_status_label(status)),
            index,
            _esc(name),
            _esc(_format_seconds(case.duration_ms)),
            "\n".join(blocks),
        )
    )


def _mapping_text(values: Any) -> str:
    if not isinstance(values, dict):
        return _format_value(values)
    return "\n".join("%s: %s" % (key, value) for key, value in values.items())


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(value)


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="%(lang)s">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%(title)s</title>
<style>
:root {
  --bg: #f6f7f9;
  --panel: #ffffff;
  --text: #1f2430;
  --muted: #6b7280;
  --line: #e3e6ec;
  --accent: #2f6fed;
  --passed: #1a7f4b;
  --failed: #c0392b;
  --error: #b45309;
  --incomplete: #7c3aed;
  --skipped: #6b7280;
  --code-bg: #f2f4f8;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14161b;
    --panel: #1c1f26;
    --text: #e6e8ee;
    --muted: #9aa3b2;
    --line: #2b303a;
    --accent: #6f9dff;
    --passed: #4ade80;
    --failed: #f87171;
    --error: #fbbf24;
    --incomplete: #c4b5fd;
    --skipped: #9aa3b2;
    --code-bg: #23262e;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 24px;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  font-size: 14px;
  line-height: 1.6;
}
h1 { font-size: 22px; margin: 0 0 6px; }
h2 { font-size: 17px; margin: 28px 0 10px; border-left: 4px solid var(--accent); padding-left: 8px; }
h3 { font-size: 14px; margin: 16px 0 6px; color: var(--muted); }
h4 { font-size: 13px; margin: 12px 0 4px; color: var(--muted); font-weight: 600; }
p { margin: 6px 0; }
code { font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace; font-size: 12.5px; }
.subtitle { color: var(--muted); margin: 0 0 16px; }
.summary { display: flex; flex-wrap: wrap; gap: 10px; }
.card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 10px 14px;
  min-width: 110px;
  display: flex;
  flex-direction: column;
}
.card-k { color: var(--muted); font-size: 12px; }
.card-v { font-size: 18px; font-weight: 600; word-break: break-all; }
.card-passed .card-v { color: var(--passed); }
.card-failed .card-v { color: var(--failed); }
.card-error .card-v { color: var(--error); }
.card-incomplete .card-v { color: var(--incomplete); }
.card-skipped .card-v { color: var(--skipped); }
table { border-collapse: collapse; width: 100%%; background: var(--panel); }
.overview, .assertions { border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
.overview thead th, .assertions thead th { background: var(--code-bg); font-weight: 600; }
.overview tbody tr:last-child td, .assertions tbody tr:last-child td { border-bottom: none; }
.kv { border: 1px solid var(--line); border-radius: 8px; }
.kv th { width: 130px; color: var(--muted); font-weight: 500; }
.assertions tr.bad td { background: rgba(192, 57, 43, 0.08); }
.assertions tr.ok td:last-child { color: var(--muted); }
.block {
  background: var(--code-bg);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 10px;
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
}
.block.error { border-color: var(--failed); color: var(--failed); }
details.case {
  background: var(--panel);
  border: 1px solid var(--line);
  border-left-width: 5px;
  border-radius: 8px;
  margin: 10px 0;
  padding: 8px 12px;
}
details.case.status-passed { border-left-color: var(--passed); }
details.case.status-failed { border-left-color: var(--failed); }
details.case.status-error { border-left-color: var(--error); }
details.case.status-incomplete { border-left-color: var(--incomplete); }
details.case.status-skipped { border-left-color: var(--skipped); }
details.case > summary { cursor: pointer; display: flex; gap: 10px; align-items: center; }
.case-name { flex: 1; font-weight: 600; }
.case-time { color: var(--muted); font-variant-numeric: tabular-nums; }
.badge {
  display: inline-block;
  min-width: 52px;
  text-align: center;
  border-radius: 999px;
  padding: 1px 8px;
  font-size: 12px;
  color: #fff;
  background: var(--skipped);
}
.badge-passed { background: var(--passed); }
.badge-failed { background: var(--failed); }
.badge-error { background: var(--error); }
.badge-incomplete { background: var(--incomplete); }
.badge-skipped { background: var(--skipped); }
.case-body { padding-top: 6px; border-top: 1px dashed var(--line); margin-top: 8px; }
.muted { color: var(--muted); }
footer { margin-top: 28px; color: var(--muted); font-size: 12px; }
@media print {
  body { background: #fff; color: #000; padding: 0; font-size: 11px; }
  @page { margin: 12mm; }
  details.case { break-inside: avoid; page-break-inside: avoid; }
  details.case > summary { list-style: none; }
  /* 打印时尽量展开折叠内容，保证 PDF 里能看到完整明细 */
  details.case:not([open]) > .case-body { display: block; }
  .summary { gap: 6px; }
  .card { border-color: #999; }
}
</style>
</head>
<body class="run-%(status_class)s">
<header>
  <h1>%(heading)s</h1>
  <p class="subtitle">%(subtitle)s</p>
  <section class="summary">
%(cards)s
  </section>
</header>
<main>
  <h2>用例总览</h2>
  <table class="overview">
    <thead><tr><th>#</th><th>用例</th><th>状态</th><th>耗时</th><th>断言（通过/总数）</th></tr></thead>
    <tbody>
%(overview)s
    </tbody>
  </table>
  <h2>用例明细</h2>
%(details)s
%(logs)s</main>
<footer>由 HapiTest 生成 · 报告为静态单文件，无脚本、无外部资源。</footer>
</body>
</html>
"""


def _safe_filename(name: str) -> str:
    """文件名安全化：只保留字母/数字/下划线/连字符，杜绝路径穿越与 ``..``。"""
    text = _SAFE_FILENAME_RE.sub("_", str(name or "run")).strip("_-")
    return (text or "run")[:80]
