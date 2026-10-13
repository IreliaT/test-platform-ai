"""``hapitest.report`` 的单元测试：JUnit XML 合法性、JSON 往返、HTML 转义、摘要与落盘。"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from hapitest import report
from hapitest.models import AssertionResult, CaseResult, RunResult
from hapitest.report import summarize_text, to_html_report, to_json_report, to_junit_xml, write_reports

TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
TIME_RE = re.compile(r"^\d+\.\d{3}$")


def _assertion(
    index: int,
    passed: bool,
    expected: Any,
    actual: Any,
    describe: str = "status_code eq 200",
    message: str = "",
) -> AssertionResult:
    return AssertionResult(
        index=index,
        type="status_code",
        operator="eq",
        target="status_code",
        expected=expected,
        actual=actual,
        passed=passed,
        message=message,
        describe=describe,
    )


def build_run() -> RunResult:
    """构造一条覆盖全部 5 种状态的运行结果。"""
    run = RunResult(
        id="run_ab12",
        project_id="demo",
        status="failed",
        trigger="manual",
        suite_name="冒烟套件",
        env_name="测试环境",
        duration_ms=2300,
        started_at="2026-03-01T02:03:04Z",
        finished_at="2026-03-01T02:03:06Z",
        fingerprint="fp-abc123",
        logs=["准备环境完成", "开始执行"],
    )
    run.case_results = [
        CaseResult(
            case_id=1,
            case_name="登录成功",
            status="passed",
            duration_ms=120,
            request={
                "method": "GET",
                "url": "http://127.0.0.1:8000/ok",
                "headers": {"Accept": "*/*", "Authorization": "******"},
                "body": "",
                "body_type": "none",
                "folder": "认证",
            },
            response={
                "ok": True,
                "status": 200,
                "headers": {"Content-Type": "application/json"},
                "body_text": '{"code": 0, "msg": "ok"}',
                "duration_ms": 120,
                "size_bytes": 24,
                "truncated": False,
                "error": "",
            },
            assertions=[_assertion(0, True, 200, 200)],
            extracted={"token": "******"},
        ),
        CaseResult(
            case_id=2,
            case_name="查询失败",
            status="failed",
            duration_ms=430,
            request={
                "method": "GET",
                "url": "http://127.0.0.1:8000/error",
                "headers": {"Accept": "*/*"},
                "body": "",
                "body_type": "none",
                "folder": "订单",
            },
            response={
                "ok": True,
                "status": 500,
                "headers": {"Content-Type": "application/json"},
                "body_text": '{"code": 5000, "msg": "boom"}',
                "duration_ms": 430,
                "size_bytes": 28,
                "truncated": False,
                "error": "",
            },
            assertions=[
                _assertion(0, True, 500, 500),
                _assertion(1, False, 0, 5000, describe="json_path[$.code] eq 0", message="期望 0，实际 5000"),
            ],
            logs=["第 1 次断言失败 1 条，重试"],
        ),
        CaseResult(
            case_id=3,
            case_name="地址不可达",
            status="error",
            duration_ms=1500,
            request={
                "method": "GET",
                "url": "http://127.0.0.1:1/ok",
                "headers": {},
                "body": "",
                "body_type": "none",
            },
            response={"ok": False, "status": None, "error": "连接被拒绝: [Errno 61] Connection refused"},
            error="连接被拒绝: [Errno 61] Connection refused",
            logs=["第 1 次尝试失败"],
        ),
        CaseResult(
            case_id=4,
            case_name="缺少断言",
            status="incomplete",
            duration_ms=0,
            request={"method": "GET", "url": "http://127.0.0.1:8000/ok", "headers": {}, "body_type": "none"},
            response={"ok": True, "status": 200, "body_text": "{}", "size_bytes": 2},
            error="用例没有可用断言",
        ),
        CaseResult(
            case_id=5,
            case_name="停用的用例",
            status="skipped",
            duration_ms=0,
            request={"method": "GET", "url": "http://127.0.0.1:8000/ok", "headers": {}, "body_type": "none"},
            response={},
            error="用例已停用",
        ),
    ]
    run.recount()
    run.status = "failed"
    return run


def find_case(root: ET.Element, name: str) -> ET.Element:
    for node in root.iter("testcase"):
        if node.get("name") == name:
            return node
    raise AssertionError("未找到用例节点: %s" % name)


class JUnitXmlTest(unittest.TestCase):
    """JUnit XML：可解析、字段齐全、状态映射正确。"""

    def setUp(self) -> None:
        self.run = build_run()
        self.xml = to_junit_xml(self.run)
        self.root = ET.fromstring(self.xml)

    def test_root_is_testsuites_with_totals(self) -> None:
        self.assertEqual("testsuites", self.root.tag)
        self.assertEqual("5", self.root.get("tests"))
        self.assertEqual("1", self.root.get("failures"))
        # error + incomplete 都计入 errors（CI 不能把未完成当通过）
        self.assertEqual("2", self.root.get("errors"))
        self.assertEqual("1", self.root.get("skipped"))

    def test_single_testsuite_with_required_attributes(self) -> None:
        suites = self.root.findall("testsuite")
        self.assertEqual(1, len(suites))
        suite = suites[0]
        for attribute in ("name", "tests", "failures", "errors", "skipped", "time", "timestamp", "hostname"):
            self.assertIsNotNone(suite.get(attribute), "testsuite 缺少属性 %s" % attribute)
        self.assertEqual("冒烟套件 · 测试环境", suite.get("name"))
        self.assertEqual("5", suite.get("tests"))
        self.assertEqual("1", suite.get("failures"))
        self.assertEqual("2", suite.get("errors"))
        self.assertEqual("1", suite.get("skipped"))
        self.assertEqual("2.300", suite.get("time"))
        self.assertTrue(suite.get("hostname"))

    def test_timestamp_has_no_timezone_suffix(self) -> None:
        suite = self.root.find("testsuite")
        timestamp = suite.get("timestamp")
        self.assertNotIn("Z", timestamp)
        self.assertNotIn("+", timestamp)
        self.assertRegex(timestamp, TIMESTAMP_RE)
        self.assertEqual("2026-03-01T02:03:04", timestamp)

    def test_testcase_attributes(self) -> None:
        cases = self.root.findall(".//testcase")
        self.assertEqual(5, len(cases))
        for node in cases:
            self.assertTrue(node.get("classname"), "testcase 必须有 classname")
            self.assertTrue(node.get("name"))
            self.assertRegex(node.get("time"), TIME_RE)

        login = find_case(self.root, "登录成功")
        self.assertEqual("冒烟套件 · 测试环境.认证", login.get("classname"))
        self.assertEqual("0.120", login.get("time"))

        failed = find_case(self.root, "查询失败")
        self.assertEqual("冒烟套件 · 测试环境.订单", failed.get("classname"))
        # 没有 folder 时归入未分组
        self.assertEqual("冒烟套件 · 测试环境.未分组", find_case(self.root, "地址不可达").get("classname"))

    def test_status_elements(self) -> None:
        passed = find_case(self.root, "登录成功")
        self.assertIsNone(passed.find("failure"))
        self.assertIsNone(passed.find("error"))
        self.assertIsNone(passed.find("skipped"))

        failed = find_case(self.root, "查询失败")
        failure = failed.find("failure")
        self.assertIsNotNone(failure)
        self.assertEqual("assertion", failure.get("type"))
        self.assertIn("期望 0，实际 5000", failure.text or "")
        self.assertIn("json_path[$.code] eq 0", failure.text or "")

        errored = find_case(self.root, "地址不可达")
        error = errored.find("error")
        self.assertIsNotNone(error)
        self.assertEqual("request", error.get("type"))
        self.assertIn("连接被拒绝", error.get("message"))

        incomplete = find_case(self.root, "缺少断言")
        incomplete_error = incomplete.find("error")
        self.assertIsNotNone(incomplete_error)
        # incomplete 必须是 error 而不是 skipped，否则 CI 会误判为通过
        self.assertEqual("incomplete", incomplete_error.get("type"))
        self.assertIsNone(incomplete.find("skipped"))

        skipped = find_case(self.root, "停用的用例")
        self.assertIsNotNone(skipped.find("skipped"))
        self.assertIsNone(skipped.find("error"))

    def test_system_out_and_system_err(self) -> None:
        failed = find_case(self.root, "查询失败")
        system_out = failed.find("system-out").text or ""
        self.assertIn("[请求] GET http://127.0.0.1:8000/error", system_out)
        self.assertIn("状态 500", system_out)
        self.assertIn('{"code": 5000, "msg": "boom"}', system_out)

        system_err = failed.find("system-err").text or ""
        self.assertIn("第 1 次断言失败 1 条，重试", system_err)

        errored = find_case(self.root, "地址不可达")
        self.assertIn("连接被拒绝", errored.find("system-err").text or "")

        # 敏感头必须已脱敏（runner 落库前已脱敏，报告层不再还原）
        login_out = find_case(self.root, "登录成功").find("system-out").text or ""
        self.assertIn("******", login_out)
        self.assertNotIn("Bearer ", login_out)

    def test_properties_present(self) -> None:
        properties = {
            node.get("name"): node.get("value") for node in self.root.findall(".//property")
        }
        self.assertEqual("HapiTest", properties.get("platform"))
        self.assertEqual("demo", properties.get("project"))
        self.assertEqual("run_ab12", properties.get("run_id"))
        self.assertEqual("manual", properties.get("trigger"))
        self.assertEqual("failed", properties.get("status"))
        self.assertEqual("fp-abc123", properties.get("fingerprint"))
        self.assertEqual("5", properties.get("total"))

    def test_long_body_is_capped(self) -> None:
        run = build_run()
        run.case_results[0].response["body_text"] = "x" * 20000
        xml = to_junit_xml(run)
        root = ET.fromstring(xml)
        system_out = find_case(root, "登录成功").find("system-out").text or ""
        self.assertLess(len(system_out), report.SYSTEM_OUT_LIMIT + 200)

    def test_illegal_control_characters_are_stripped(self) -> None:
        run = build_run()
        run.case_results[0].case_name = "非法\u0000字符\u0007测试"
        run.case_results[0].error = "换行\u000b保留\t制表"
        run.case_results[0].logs = ["日志\u001b[31m着色"]
        xml = to_junit_xml(run)
        self.assertNotIn("\u0000", xml)
        self.assertNotIn("\u001b", xml)
        root = ET.fromstring(xml)
        self.assertEqual("非法字符测试", find_case(root, "非法字符测试").get("name"))

    def test_xml_escaping_round_trip(self) -> None:
        run = build_run()
        run.case_results[0].case_name = '名称 & <标签> "引号"'
        xml = to_junit_xml(run)
        root = ET.fromstring(xml)
        self.assertEqual('名称 & <标签> "引号"', find_case(root, '名称 & <标签> "引号"').get("name"))

    def test_empty_run_is_exportable(self) -> None:
        root = ET.fromstring(to_junit_xml(RunResult(id="run_empty", status="incomplete")))
        suite = root.find("testsuite")
        self.assertEqual("0", suite.get("tests"))
        self.assertEqual("0", suite.get("failures"))
        self.assertEqual("0", suite.get("errors"))
        self.assertEqual(0, len(list(root.iter("testcase"))))
        self.assertRegex(suite.get("timestamp"), TIMESTAMP_RE)

    def test_timestamp_falls_back_when_started_at_missing(self) -> None:
        run = build_run()
        run.started_at = ""
        suite = ET.fromstring(to_junit_xml(run)).find("testsuite")
        self.assertRegex(suite.get("timestamp"), TIMESTAMP_RE)


class JsonReportTest(unittest.TestCase):
    """JSON 报告：与模型 to_dict 完全一致，中文不转义。"""

    def test_round_trip(self) -> None:
        run = build_run()
        text = to_json_report(run)
        data = json.loads(text)
        self.assertEqual(run.to_dict(), data)
        self.assertEqual("run_ab12", data["id"])
        self.assertEqual(5, len(data["case_results"]))
        self.assertEqual("查询失败", data["case_results"][1]["case_name"])
        self.assertIn("查询失败", text)  # 中文不转义

    def test_indented_output(self) -> None:
        text = to_json_report(build_run())
        self.assertIn('\n  "id": "run_ab12"', text)


class HtmlReportTest(unittest.TestCase):
    """HTML 报告：自包含、可读、全部转义。"""

    def setUp(self) -> None:
        self.run = build_run()
        self.html = to_html_report(self.run, project_name="示例项目")

    def test_self_contained_without_external_resources(self) -> None:
        lowered = self.html.lower()
        self.assertTrue(lowered.startswith("<!doctype html>"))
        self.assertIn('<meta charset="utf-8">', lowered)
        self.assertIn("<style>", lowered)
        self.assertNotIn("<script", lowered)
        self.assertNotIn("<link", lowered)
        self.assertNotIn("<img", lowered)
        self.assertNotIn("cdn.", lowered)
        self.assertNotIn("https://", lowered)

    def test_summary_cards_and_labels(self) -> None:
        for label in ("总数", "通过", "失败", "错误", "未完成", "跳过", "耗时", "触发方式", "环境", "开始时间", "运行 ID"):
            self.assertIn(label, self.html)
        self.assertIn("run_ab12", self.html)
        self.assertIn("测试环境", self.html)
        self.assertIn("2026-03-01T02:03:04Z", self.html)

    def test_case_details_blocks(self) -> None:
        self.assertIn("<details", self.html)
        self.assertEqual(5, self.html.count('class="case status-'))
        self.assertIn("登录成功", self.html)
        self.assertIn("查询失败", self.html)
        self.assertIn("http://127.0.0.1:8000/error", self.html)
        self.assertIn("期望", self.html)
        self.assertIn("实际", self.html)
        self.assertIn("token", self.html)  # 提取变量（已脱敏）
        self.assertIn("第 1 次断言失败 1 条，重试", self.html)

    def test_failed_cases_open_by_default(self) -> None:
        self.assertIn("status-failed\" open", self.html)

    def test_print_styles_present(self) -> None:
        self.assertIn("@media print", self.html)
        self.assertIn("@page", self.html)

    def test_title_override(self) -> None:
        html = to_html_report(self.run, title="自定义标题")
        self.assertIn("<title>自定义标题</title>", html)

    def test_html_escapes_script_payloads(self) -> None:
        run = build_run()
        case = run.case_results[0]
        case.case_name = "<script>alert('name')</script>"
        case.request["headers"] = {"X-Test": "<script>header()</script>"}
        case.request["body"] = '{"q": "<script>body()</script>"}'
        case.response["body_text"] = "<script>response()</script>"
        case.assertions = [_assertion(0, False, "<script>expected()</script>", "<img src=x onerror=alert(1)>")]
        case.error = "<script>error()</script>"
        case.logs = ["<script>log()</script>"]
        run.logs = ["<script>runlog()</script>"]

        html = to_html_report(run, project_name="<script>project()</script>")

        self.assertNotIn("<script", html)
        self.assertIn("&lt;script&gt;alert(&#x27;name&#x27;)&lt;/script&gt;", html)
        self.assertIn("&lt;script&gt;header()&lt;/script&gt;", html)
        self.assertIn("&lt;script&gt;response()&lt;/script&gt;", html)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", html)
        self.assertIn("&lt;script&gt;error()&lt;/script&gt;", html)
        self.assertIn("&lt;script&gt;log()&lt;/script&gt;", html)
        self.assertIn("&lt;script&gt;runlog()&lt;/script&gt;", html)
        self.assertIn("&lt;script&gt;project()&lt;/script&gt;", html)

    def test_empty_run_is_exportable(self) -> None:
        html = to_html_report(RunResult(id="run_empty", status="incomplete"))
        self.assertIn("没有用例记录", html)
        self.assertIn("总数", html)


class WriteReportsTest(unittest.TestCase):
    """write_reports：目录布局、返回路径与文件内容。"""

    def test_writes_three_files(self) -> None:
        run = build_run()
        with tempfile.TemporaryDirectory(prefix="hapitest-report-") as tmp:
            paths = write_reports(run, tmp, project_name="demo")
            self.assertEqual({"xml", "html", "json"}, set(paths))
            for key in ("xml", "html", "json"):
                self.assertIsInstance(paths[key], str)
                self.assertTrue(Path(paths[key]).is_file(), paths[key])

            self.assertEqual(str(Path(tmp) / "demo" / "run-run_ab12.xml"), paths["xml"])
            self.assertEqual(str(Path(tmp) / "demo" / "run-run_ab12.html"), paths["html"])
            self.assertEqual(str(Path(tmp) / "demo" / "run-run_ab12.json"), paths["json"])

            xml_text = Path(paths["xml"]).read_text(encoding="utf-8")
            ET.fromstring(xml_text)
            self.assertIn('type="assertion"', xml_text)

            html_text = Path(paths["html"]).read_text(encoding="utf-8")
            self.assertIn("demo 测试报告", html_text)
            self.assertIn("<details", html_text)

            data = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
            self.assertEqual(run.id, data["id"])

    def test_default_directory_is_reports(self) -> None:
        run = build_run()
        with tempfile.TemporaryDirectory(prefix="hapitest-report-") as tmp:
            paths = write_reports(run, tmp)
            self.assertEqual(str(Path(tmp) / "reports" / "run-run_ab12.xml"), paths["xml"])
            self.assertTrue(Path(paths["xml"]).is_file())

    def test_run_id_is_sanitized(self) -> None:
        run = build_run()
        run.id = "../../etc/passwd"
        with tempfile.TemporaryDirectory(prefix="hapitest-report-") as tmp:
            paths = write_reports(run, tmp, project_name="demo")
            written = Path(paths["xml"])
            self.assertTrue(written.is_file())
            # 不得逃出目标目录
            self.assertEqual(str(Path(tmp) / "demo"), str(written.parent))
            self.assertNotIn("..", written.name)

    def test_accepts_path_object(self) -> None:
        run = build_run()
        with tempfile.TemporaryDirectory(prefix="hapitest-report-") as tmp:
            paths = write_reports(run, Path(tmp) / "nested" / "deep", project_name="p")
            self.assertTrue(Path(paths["html"]).is_file())


class SummarizeTextTest(unittest.TestCase):
    """summarize_text：与契约示例格式一致。"""

    def test_matches_contract_example(self) -> None:
        run = RunResult(id="run_ab12", status="failed", duration_ms=2300)
        run.case_results = [
            CaseResult(case_name="c%d" % index, status="passed") for index in range(8)
        ] + [
            CaseResult(case_name="f", status="failed"),
            CaseResult(case_name="e", status="error"),
        ]
        self.assertEqual(
            "运行 run_ab12 通过 8/10，失败 1，错误 1，未完成 0，耗时 2.3s → failed",
            summarize_text(run),
        )

    def test_skipped_is_shown_when_present(self) -> None:
        run = RunResult(id="run_x", status="incomplete", duration_ms=100)
        run.case_results = [CaseResult(case_name="a", status="passed"), CaseResult(case_name="b", status="skipped")]
        text = summarize_text(run)
        self.assertIn("跳过 1", text)
        self.assertTrue(text.endswith("→ incomplete"))

    def test_empty_run(self) -> None:
        text = summarize_text(RunResult(id="run_empty", status="incomplete"))
        self.assertIn("通过 0/0", text)
        self.assertTrue(text.endswith("→ incomplete"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
