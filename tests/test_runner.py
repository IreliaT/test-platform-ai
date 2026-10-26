"""执行引擎测试：五种终态、变量串联、幂等、取消。"""

from __future__ import annotations

import unittest

from hapitest.errors import ConflictError
from hapitest.models import (
    AssertionSpec,
    CaseSpec,
    EnvironmentSpec,
    ExtractorSpec,
    KeyValue,
    RequestSpec,
    StepSpec,
)
from hapitest.repository import ProjectRegistry
from hapitest.runner import RunManager, RunRequest
from tests.support import LocalHttpServer, TempConfig


class RunnerStatusTest(unittest.TestCase):
    """每一种终态都必须由真实请求产生，不允许"没报错就算通过"。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._server_ctx = LocalHttpServer()
        cls.server = cls._server_ctx.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._server_ctx.__exit__(None, None, None)

    def setUp(self) -> None:
        self._ctx = TempConfig()
        self.config = self._ctx.__enter__()
        self.registry = ProjectRegistry(self.config)
        self.record = self.registry.create(name="执行器测试", project_id="runner", base_url=self.server.base_url)
        self.repo = self.registry.repository(self.record.id)
        self.environment = self.repo.resolve_environment(None)
        self.environment.base_url = self.server.base_url
        self.repo.update_environment(self.environment.id, self.environment)
        self.manager = RunManager(self.config, self.registry)
        self.manager.start()

    def tearDown(self) -> None:
        self.manager.stop()
        self._ctx.__exit__(None, None, None)

    def _run_case(self, case: CaseSpec, env_id=None):
        saved = self.repo.create_case(case)
        run = self.manager.run_sync(
            RunRequest(project_id=self.record.id, case_ids=[saved.id], env_id=env_id or self.environment.id, trigger="api")
        )
        self.assertEqual(len(run.case_results), 1)
        return run, run.case_results[0]

    def test_passed(self) -> None:
        run, result = self._run_case(
            CaseSpec(
                name="健康检查通过",
                request=RequestSpec(method="GET", path="/ok"),
                assertions=[
                    AssertionSpec(type="status_code", operator="eq", expected=200),
                    AssertionSpec(type="json_path", operator="eq", path="$.code", expected=0),
                    AssertionSpec(type="response_time", operator="lt", expected=5000),
                ],
            )
        )
        self.assertEqual(result.status, "passed")
        self.assertEqual(run.status, "passed")
        self.assertEqual(run.exit_code, 0)
        self.assertEqual(result.response["status"], 200)
        self.assertEqual(result.request["method"], "GET")

    def test_execution_log_records_request_response_and_result(self) -> None:
        """执行日志必须能回答"跑了什么、回了什么、结果如何"。"""
        run, result = self._run_case(
            CaseSpec(
                name="执行日志",
                request=RequestSpec(method="GET", path="/ok"),
                assertions=[AssertionSpec(type="status_code", operator="eq", expected=200)],
            )
        )
        logs = "\n".join(result.logs)
        self.assertIn("开始执行用例", logs)
        self.assertIn("/ok", logs)
        self.assertIn("← HTTP 200", logs)
        self.assertIn("断言 1/1", logs)
        self.assertIn("结果：通过", logs)
        # 运行级日志也要能看出跑了哪条用例
        self.assertTrue(any("执行日志" in line and "通过" in line for line in run.logs), run.logs)

    def test_execution_log_marks_failed_assertion(self) -> None:
        _, result = self._run_case(
            CaseSpec(
                name="日志断言失败",
                request=RequestSpec(method="GET", path="/ok"),
                assertions=[AssertionSpec(type="json_path", operator="eq", path="$.code", expected=999)],
            )
        )
        logs = "\n".join(result.logs)
        self.assertIn("✗ 断言 1/1", logs)
        self.assertIn("结果：失败", logs)

    def test_execution_log_masks_secret_header(self) -> None:
        _, result = self._run_case(
            CaseSpec(
                name="日志脱敏",
                request=RequestSpec(
                    method="GET",
                    path="/echo",
                    headers=[KeyValue(name="X-Token", value="real-secret")],
                ),
                assertions=[AssertionSpec(type="status_code", operator="eq", expected=200)],
            )
        )
        logs = "\n".join(result.logs)
        self.assertIn("X-Token=******", logs)
        self.assertNotIn("real-secret", logs)

    def test_execution_log_for_incomplete_case(self) -> None:
        """缺条件的用例也要说明原因，不能只有一句 error。"""
        case = CaseSpec(name="没有断言", request=RequestSpec(method="GET", path="/ok"), assertions=[])
        saved = self.repo.create_case(case)
        run = self.manager.run_sync(
            RunRequest(project_id=self.record.id, case_ids=[saved.id], env_id=self.environment.id, trigger="api")
        )
        result = run.case_results[0]
        logs = "\n".join(result.logs)
        self.assertEqual(result.status, "incomplete")
        self.assertIn("未定义任何断言", logs)
        self.assertIn("结果：未完成", logs)

    def test_failed_assertion(self) -> None:
        run, result = self._run_case(
            CaseSpec(
                name="断言失败",
                request=RequestSpec(method="GET", path="/ok"),
                assertions=[AssertionSpec(type="json_path", operator="eq", path="$.code", expected=999)],
            )
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.exit_code, 1)
        failed = [item for item in result.assertions if not item.passed]
        self.assertEqual(len(failed), 1)
        self.assertIn("$.code", failed[0].describe)

    def test_no_assertions_is_incomplete(self) -> None:
        run, result = self._run_case(
            CaseSpec(name="没有断言", request=RequestSpec(method="GET", path="/ok"), assertions=[])
        )
        self.assertEqual(result.status, "incomplete")
        self.assertIn("断言", result.error)

    def test_missing_base_url_is_incomplete(self) -> None:
        # project 级 base_url 会被继承，这里全部清空以验证"缺条件"分支
        self.registry.update(self.record.id, base_url="")
        empty = self.repo.create_environment(EnvironmentSpec(name="空环境", base_url=""))
        _, result = self._run_case(
            CaseSpec(
                name="没有地址",
                request=RequestSpec(method="GET", path="/ok", base_url=""),
                assertions=[AssertionSpec(type="status_code", operator="eq", expected=200)],
            ),
            env_id=empty.id,
        )
        self.assertEqual(result.status, "incomplete")
        self.assertIn("base_url", result.error)

    def test_unreachable_is_error(self) -> None:
        run, result = self._run_case(
            CaseSpec(
                name="地址不可达",
                request=RequestSpec(method="GET", path="/ok", base_url="http://127.0.0.1:1", timeout_ms=1500),
                assertions=[AssertionSpec(type="status_code", operator="eq", expected=200)],
            )
        )
        self.assertEqual(result.status, "error")
        self.assertEqual(run.status, "error")
        self.assertEqual(run.exit_code, 2)
        self.assertTrue(result.error)

    def test_disabled_is_skipped(self) -> None:
        run, result = self._run_case(
            CaseSpec(
                name="停用的用例",
                enabled=False,
                request=RequestSpec(method="GET", path="/ok"),
                assertions=[AssertionSpec(type="status_code", operator="eq", expected=200)],
            )
        )
        self.assertEqual(result.status, "skipped")
        self.assertEqual(run.status, "incomplete")

    def test_retry_then_pass(self) -> None:
        run, result = self._run_case(
            CaseSpec(
                name="依赖 /json",
                request=RequestSpec(method="GET", path="/json"),
                retries=1,
                assertions=[AssertionSpec(type="json_path", operator="exists", path="$.data.items")],
            )
        )
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.attempts, 1)

    def test_suite_mixed_statuses(self) -> None:
        passed = self.repo.create_case(
            CaseSpec(
                name="通过",
                request=RequestSpec(method="GET", path="/ok"),
                assertions=[AssertionSpec(type="status_code", operator="eq", expected=200)],
            )
        )
        failed = self.repo.create_case(
            CaseSpec(
                name="失败",
                request=RequestSpec(method="GET", path="/ok"),
                assertions=[AssertionSpec(type="status_code", operator="eq", expected=500)],
            )
        )
        from hapitest.models import SuiteSpec

        suite = self.repo.create_suite(SuiteSpec(name="混合套件", case_ids=[passed.id, failed.id]))
        run = self.manager.run_sync(RunRequest(project_id=self.record.id, suite_id=suite.id, trigger="api"))
        self.assertEqual(run.total, 2)
        self.assertEqual(run.passed, 1)
        self.assertEqual(run.failed, 1)
        self.assertEqual(run.status, "failed")
        self.assertEqual([item.case_name for item in run.case_results], ["通过", "失败"])

    def test_stop_on_failure(self) -> None:
        from hapitest.models import SuiteSpec

        failed = self.repo.create_case(
            CaseSpec(
                name="先失败",
                request=RequestSpec(method="GET", path="/ok"),
                assertions=[AssertionSpec(type="status_code", operator="eq", expected=500)],
            )
        )
        second = self.repo.create_case(
            CaseSpec(
                name="不该执行",
                request=RequestSpec(method="GET", path="/ok"),
                assertions=[AssertionSpec(type="status_code", operator="eq", expected=200)],
            )
        )
        suite = self.repo.create_suite(
            SuiteSpec(name="失败即停", case_ids=[failed.id, second.id], failure_strategy="stop")
        )
        run = self.manager.run_sync(RunRequest(project_id=self.record.id, suite_id=suite.id, trigger="api"))
        self.assertEqual(run.total, 1)
        self.assertEqual(run.case_results[0].case_name, "先失败")

    def test_setup_extraction_chaining(self) -> None:
        """前置步骤提取的变量要能在主请求里使用（典型登录串联场景）。"""
        case = CaseSpec(
            name="登录后带令牌访问",
            request=RequestSpec(
                method="GET",
                path="/echo",
                headers=[KeyValue(name="X-Trace", value="${traceId}")],
            ),
            setup=[
                StepSpec(
                    name="登录",
                    request=RequestSpec(method="GET", path="/json"),
                    extractors=[ExtractorSpec(name="traceId", source="json_path", expression="$.data.nested.token")],
                )
            ],
            assertions=[
                AssertionSpec(type="status_code", operator="eq", expected=200),
                AssertionSpec(type="body_text", operator="contains", expected="abc"),
            ],
        )
        run, result = self._run_case(case)
        self.assertEqual(result.status, "passed", result.error)
        self.assertEqual(len(result.steps), 1)
        self.assertEqual(result.steps[0]["status"], "passed")
        self.assertEqual(result.steps[0]["extracted"].get("traceId"), "abc")
        echoed = result.response.get("body_json") or {}
        self.assertEqual(echoed.get("headers", {}).get("X-Trace"), "abc")

    def test_secret_header_is_masked_in_recorded_response(self) -> None:
        """响应记录里，命中敏感词的请求头必须被掩码（安全相关行为）。"""
        case = CaseSpec(
            name="敏感头脱敏",
            request=RequestSpec(
                method="GET",
                path="/echo",
                headers=[KeyValue(name="X-Token", value="real-secret")],
            ),
            assertions=[AssertionSpec(type="status_code", operator="eq", expected=200)],
        )
        _, result = self._run_case(case)
        self.assertEqual(result.status, "passed", result.error)
        echoed = result.response.get("body_json") or {}
        self.assertEqual(echoed.get("headers", {}).get("X-Token"), "******")
        self.assertEqual(result.request.get("headers", {}).get("X-Token"), "******")

    def test_secret_extraction_is_masked_in_history(self) -> None:
        case = CaseSpec(
            name="提取敏感变量",
            request=RequestSpec(method="GET", path="/json"),
            extractors=[ExtractorSpec(name="access_token", source="json_path", expression="$.data.nested.token")],
            assertions=[AssertionSpec(type="status_code", operator="eq", expected=200)],
        )
        _, result = self._run_case(case)
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.extracted.get("access_token"), "******")

    def test_auth_bearer_injected(self) -> None:
        from hapitest.models import AuthSpec

        case = CaseSpec(
            name="带 Bearer 令牌",
            request=RequestSpec(
                method="GET",
                path="/echo",
                auth=AuthSpec(type="bearer", token="tok-123"),
            ),
            assertions=[AssertionSpec(type="body_text", operator="contains", expected="Bearer tok-123")],
        )
        _, result = self._run_case(case)
        self.assertEqual(result.status, "passed", result.error)
        self.assertEqual(result.request["headers"].get("Authorization"), "******")

    def test_idempotent_request_id(self) -> None:
        case = self.repo.create_case(
            CaseSpec(
                name="幂等用例",
                request=RequestSpec(method="GET", path="/ok"),
                assertions=[AssertionSpec(type="status_code", operator="eq", expected=200)],
            )
        )
        request = RunRequest(project_id=self.record.id, case_ids=[case.id], trigger="api", request_id="rid-1",
                             variables={"user_token": "ephemeral-test-token"})
        first = self.manager.submit(request)
        self.manager.wait(self.record.id, first.id, timeout_s=30)
        again = self.manager.find_existing(request)
        self.assertIsNotNone(again)
        self.assertEqual(again.id, first.id)
        self.assertNotIn("ephemeral-test-token", self.manager._payload_path(self.record.id, first.id).read_text())
        with self.assertRaises(ConflictError):
            self.manager.find_existing(RunRequest(project_id=self.record.id, case_ids=[case.id], trigger="api",
                request_id="rid-1", variables={"user_token": "different-test-token"}))
        with self.assertRaises(ConflictError):
            self.manager.find_existing(
                RunRequest(project_id=self.record.id, case_ids=[case.id], trigger="api", request_id="rid-1", env_id=999)
            )

    def test_fingerprint_recorded(self) -> None:
        case = self.repo.create_case(
            CaseSpec(
                name="指纹用例",
                request=RequestSpec(method="GET", path="/ok"),
                assertions=[AssertionSpec(type="status_code", operator="eq", expected=200)],
            )
        )
        run = self.manager.run_sync(RunRequest(project_id=self.record.id, case_ids=[case.id], trigger="api"))
        self.assertTrue(run.fingerprint)
        self.assertEqual(len(run.fingerprint), 16)

    def test_session_cookie_reused_across_cases(self) -> None:
        """同一次运行内共享 Cookie Jar：先拿会话，再带会话访问。"""
        from hapitest.models import SuiteSpec

        login = self.repo.create_case(
            CaseSpec(
                name="获取会话",
                request=RequestSpec(method="GET", path="/set-cookie"),
                assertions=[AssertionSpec(type="status_code", operator="eq", expected=200)],
            )
        )
        follow = self.repo.create_case(
            CaseSpec(
                name="带会话访问",
                request=RequestSpec(method="GET", path="/echo"),
                assertions=[
                    AssertionSpec(type="status_code", operator="eq", expected=200),
                    AssertionSpec(type="body_text", operator="contains", expected="sid=abc123"),
                ],
            )
        )
        suite = self.repo.create_suite(SuiteSpec(name="会话套件", case_ids=[login.id, follow.id]))
        run = self.manager.run_sync(RunRequest(project_id=self.record.id, suite_id=suite.id, trigger="api"))
        self.assertEqual(run.status, "passed", [item.error for item in run.case_results])

    def test_folder_recorded_for_report_grouping(self) -> None:
        from hapitest.report import to_junit_xml

        case = self.repo.create_case(
            CaseSpec(
                name="分组用例",
                folder="用户管理",
                request=RequestSpec(method="GET", path="/ok"),
                assertions=[AssertionSpec(type="status_code", operator="eq", expected=200)],
            )
        )
        run = self.manager.run_sync(RunRequest(project_id=self.record.id, case_ids=[case.id], trigger="api"))
        self.assertEqual(run.case_results[0].request.get("folder"), "用户管理")
        self.assertIn("用户管理", to_junit_xml(run))

    def test_concurrent_suite_records_every_case(self) -> None:
        """套件并发度 > 1 时，每条用例的结果都必须独立落库。"""
        from hapitest.models import SuiteSpec

        case_ids = []
        for index in range(5):
            case = self.repo.create_case(
                CaseSpec(
                    name="并发用例 %d" % index,
                    request=RequestSpec(method="GET", path="/ok"),
                    assertions=[AssertionSpec(type="status_code", operator="eq", expected=200)],
                )
            )
            case_ids.append(case.id)
        suite = self.repo.create_suite(SuiteSpec(name="并发套件", case_ids=case_ids, concurrency=3))
        run = self.manager.run_sync(RunRequest(project_id=self.record.id, suite_id=suite.id, trigger="api"))
        self.assertEqual(run.status, "passed")
        self.assertEqual(run.total, len(case_ids))
        stored = self.repo.get_run(run.id, with_cases=True)
        self.assertEqual(len(stored.case_results), len(case_ids))
        self.assertEqual(sorted(item.case_id for item in stored.case_results), sorted(case_ids))
        self.assertTrue(all(item.status == "passed" for item in stored.case_results))

    def test_queue_rejects_empty_case_list(self) -> None:
        from hapitest.errors import NotFoundError

        with self.assertRaises(NotFoundError):
            self.manager.submit(RunRequest(project_id=self.record.id, trigger="api"))


if __name__ == "__main__":
    unittest.main()
