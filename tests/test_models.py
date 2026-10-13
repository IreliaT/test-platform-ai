"""核心模型与规范化规则的测试。"""

from __future__ import annotations

import unittest

from hapitest.models import (
    AssertionSpec,
    CaseSpec,
    EndpointSpec,
    EnvironmentSpec,
    HttpResponse,
    KeyValue,
    RequestSpec,
    RunResult,
    detect_path_param_names,
    normalize_path,
    validate_assertion,
    validate_case,
)


class NormalizePathTest(unittest.TestCase):
    def test_basic(self) -> None:
        self.assertEqual(normalize_path("api/users"), "/api/users")
        self.assertEqual(normalize_path("/api/users/"), "/api/users")
        self.assertEqual(normalize_path("/api//users"), "/api/users")
        self.assertEqual(normalize_path("/api/users?page=1#x"), "/api/users")
        self.assertEqual(normalize_path("http://host:8080/api/users?x=1"), "/api/users")
        self.assertEqual(normalize_path(""), "/")

    def test_path_params(self) -> None:
        self.assertEqual(detect_path_param_names("/api/users/{id}/tags/{tagId}"), ["id", "tagId"])
        self.assertEqual(detect_path_param_names("/api/users/:id"), ["id"])
        self.assertEqual(detect_path_param_names("/api/users"), [])


class RequestSpecTest(unittest.TestCase):
    def test_round_trip(self) -> None:
        request = RequestSpec(
            method="post",
            path="/api/users",
            base_url="http://127.0.0.1:8899",
            query=[KeyValue(name="page", value="1")],
            headers=[KeyValue(name="X-Token", value="${TOKEN}")],
            body_type="json",
            body={"name": "张三"},
        )
        data = request.to_dict()
        self.assertEqual(data["method"], "POST")
        restored = RequestSpec.from_dict(data)
        self.assertEqual(restored.body, {"name": "张三"})
        self.assertEqual(restored.query[0].name, "page")
        self.assertEqual(restored.headers[0].value, "${TOKEN}")

    def test_tolerates_garbage(self) -> None:
        restored = RequestSpec.from_dict({"method": "NOT_A_METHOD", "body_type": "??", "query": ["a"]})
        self.assertEqual(restored.method, "GET")
        self.assertIn(restored.body_type, ("raw", "none"))
        self.assertEqual(len(restored.query), 1)

    def test_missing_dict(self) -> None:
        self.assertEqual(RequestSpec.from_dict(None).method, "GET")


class AssertionValidationTest(unittest.TestCase):
    def test_valid(self) -> None:
        self.assertEqual(validate_assertion(AssertionSpec(type="status_code", operator="eq", expected=200)), [])
        self.assertEqual(
            validate_assertion(AssertionSpec(type="json_path", operator="exists", path="$.data.id")), []
        )

    def test_errors(self) -> None:
        self.assertTrue(validate_assertion(AssertionSpec(type="nope", operator="eq", expected=1)))
        self.assertTrue(validate_assertion(AssertionSpec(type="json_path", operator="eq", expected=1)))
        self.assertTrue(validate_assertion(AssertionSpec(type="header", operator="eq", expected=1)))
        self.assertTrue(validate_assertion(AssertionSpec(type="status_code", operator="in", expected=200)))
        self.assertTrue(
            validate_assertion(AssertionSpec(type="status_code", operator="between", expected=[1]))
        )
        self.assertTrue(validate_assertion(AssertionSpec(type="status_code", operator="eq", expected=None)))


class CaseValidationTest(unittest.TestCase):
    def test_name_required(self) -> None:
        case = CaseSpec(name="  ", request=RequestSpec(path="/x"))
        self.assertTrue(validate_case(case))

    def test_ok(self) -> None:
        case = CaseSpec(
            name="查询用户",
            request=RequestSpec(method="GET", path="/api/users"),
            assertions=[AssertionSpec(type="status_code", operator="eq", expected=200)],
        )
        self.assertEqual(validate_case(case), [])


class EndpointTest(unittest.TestCase):
    def test_signature_normalized(self) -> None:
        endpoint = EndpointSpec(method="get", path="/api/users/")
        self.assertEqual(endpoint.signature(), "GET /api/users")


class RunResultTest(unittest.TestCase):
    def test_finalize_prefers_failure(self) -> None:
        run = RunResult(id="run_1")
        run.case_results = [
            _case_result("passed"),
            _case_result("incomplete"),
        ]
        run.recount()
        self.assertEqual(run.finalize_status(), "incomplete")
        run.case_results.append(_case_result("failed"))
        run.recount()
        self.assertEqual(run.finalize_status(), "failed")

    def test_all_passed(self) -> None:
        run = RunResult(id="run_2")
        run.case_results = [_case_result("passed"), _case_result("passed")]
        run.recount()
        self.assertEqual(run.status, "queued")
        self.assertEqual(run.finalize_status(), "passed")
        self.assertEqual(run.exit_code, 0)

    def test_no_cases_is_incomplete(self) -> None:
        run = RunResult(id="run_3")
        run.recount()
        self.assertEqual(run.finalize_status(), "incomplete")
        self.assertNotEqual(run.exit_code, 0)

    def test_serialization_keeps_incomplete(self) -> None:
        run = RunResult(id="run_4", status="incomplete", incomplete=2, total=2)
        data = run.to_dict(with_cases=False)
        self.assertEqual(data["incomplete"], 2)
        self.assertEqual(RunResult.from_dict(data).incomplete, 2)


class EnvironmentMaskTest(unittest.TestCase):
    def test_secret_values_masked_in_output(self) -> None:
        environment = EnvironmentSpec(name="测试", variables={"token": "real-token", "host": "127.0.0.1"})
        masked = environment.to_dict()
        self.assertEqual(masked["variables"]["token"], "******")
        self.assertEqual(masked["variables"]["host"], "127.0.0.1")
        self.assertEqual(environment.to_dict(reveal_secrets=True)["variables"]["token"], "real-token")


class HttpResponseTest(unittest.TestCase):
    def test_round_trip(self) -> None:
        response = HttpResponse(ok=True, status=200, headers={"Content-Type": "application/json"}, body_text="{}")
        restored = HttpResponse.from_dict(response.to_dict())
        self.assertTrue(restored.ok)
        self.assertEqual(restored.status, 200)


def _case_result(status: str):
    from hapitest.models import CaseResult

    return CaseResult(case_name="x", status=status)


if __name__ == "__main__":
    unittest.main()
