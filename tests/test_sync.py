"""导入落库（sync）与规格抓取的测试。

这一层是"业务契约 → 平台数据"的接缝，最容易出现静默失败，因此重点验证：
解析失败必须变成可读错误、重复同步必须幂等、业务数据缺失必须明确提示。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from hapitest.errors import ValidationError
from hapitest.models import AssertionSpec, CaseSpec, EndpointSpec, ImportResult, ImportStats, ParamSpec, RequestSpec
from hapitest.repository import ProjectRegistry
from hapitest.sync import apply_import, detected_hints, fetch_spec, load_import, quality_warnings
from tests.support import LocalHttpServer, TempConfig

DEMO_SPEC = Path(__file__).resolve().parent.parent / "examples" / "demo_specs" / "demo_openapi.json"
SWAGGER2_SPEC = Path(__file__).resolve().parent.parent / "examples" / "demo_specs" / "demo_swagger2.json"


def _html_handler(method, path, headers, body):
    if path.startswith("/html"):
        return 200, {"Content-Type": "text/html; charset=utf-8"}, b"<html><body>login</body></html>"
    if path.startswith("/missing"):
        return 404, {"Content-Type": "application/json"}, b'{"msg":"no"}'
    payload = json.loads(SWAGGER2_SPEC.read_text(encoding="utf-8"))
    return 200, {"Content-Type": "application/json"}, json.dumps(payload).encode("utf-8")


class LoadImportTest(unittest.TestCase):
    def test_parse_openapi_file(self) -> None:
        with TempConfig() as config:
            result = load_import(config, path=str(DEMO_SPEC))
            self.assertEqual(len(result.endpoints), 7)
            self.assertEqual(len(result.cases), 7)
            self.assertTrue(all(case.assertions for case in result.cases))

    def test_parse_swagger2_file(self) -> None:
        with TempConfig() as config:
            result = load_import(config, path=str(SWAGGER2_SPEC))
            self.assertEqual(result.source, "swagger2")
            self.assertEqual(len(result.endpoints), 7)
            hints = detected_hints(result)
            self.assertEqual(hints.get("detected_server_url"), "http://127.0.0.1:8899")

    def test_relative_path_resolves_against_project_root(self) -> None:
        with TempConfig() as config:
            result = load_import(config, path="examples/demo_specs/demo_openapi.json")
            self.assertEqual(len(result.endpoints), 7)

    def test_unknown_text_raises_validation_error(self) -> None:
        with TempConfig() as config:
            with self.assertRaises(ValidationError) as ctx:
                load_import(config, text="这不是任何已知格式")
            self.assertIn("解析失败", ctx.exception.message)

    def test_missing_file_and_directory(self) -> None:
        with TempConfig() as config:
            with self.assertRaises(ValidationError):
                load_import(config, path="/no/such/file.json")
            with self.assertRaises(ValidationError) as ctx:
                load_import(config, path="examples")
            self.assertIn("目录", ctx.exception.message)

    def test_requires_some_input(self) -> None:
        with TempConfig() as config:
            with self.assertRaises(ValidationError):
                load_import(config)

    def test_forced_source_overrides_detected(self) -> None:
        with TempConfig() as config:
            result = load_import(config, path=str(DEMO_SPEC), source="apifox")
            self.assertEqual(result.source, "apifox")
            self.assertTrue(all(endpoint.source == "apifox" for endpoint in result.endpoints))


class QualityWarningTest(unittest.TestCase):
    def test_path_param_without_example_is_reported(self) -> None:
        endpoint = EndpointSpec(
            name="查询详情",
            method="GET",
            path="/api/users/{id}",
            params=[ParamSpec(name="id", location="path", required=True, type="integer", example=None)],
        )
        result = ImportResult(endpoints=[endpoint], cases=[], stats=ImportStats(source="openapi"))
        warnings = quality_warnings(result)
        self.assertEqual(len(warnings), 1)
        self.assertIn("路径参数 id", warnings[0])

    def test_path_param_with_example_is_quiet(self) -> None:
        endpoint = EndpointSpec(
            name="查询详情",
            method="GET",
            path="/api/users/{id}",
            params=[ParamSpec(name="id", location="path", required=True, type="integer", example=1)],
        )
        result = ImportResult(endpoints=[endpoint], cases=[], stats=ImportStats(source="openapi"))
        self.assertEqual(quality_warnings(result), [])

    def test_unresolved_placeholder_in_case_reported(self) -> None:
        case = CaseSpec(name="未解析", request=RequestSpec(method="GET", path="/api/users/{id}"))
        result = ImportResult(endpoints=[], cases=[case], stats=ImportStats(source="openapi"))
        warnings = quality_warnings(result)
        self.assertTrue(any("未解析的路径参数" in item for item in warnings))


class ApplyImportTest(unittest.TestCase):
    def setUp(self) -> None:
        self._ctx = TempConfig()
        config = self._ctx.__enter__()
        self.registry = ProjectRegistry(config)
        self.registry.create(name="同步测试", project_id="sync")
        self.repo = self.registry.repository("sync")
        self.config = config

    def tearDown(self) -> None:
        self._ctx.__exit__(None, None, None)

    def test_merge_is_idempotent(self) -> None:
        first = load_import(self.config, path=str(DEMO_SPEC))
        stats = apply_import(self.repo, first, mode="merge")
        self.assertEqual(stats.created, 7)
        self.assertEqual(stats.cases_created, 7)
        self.assertEqual(self.repo.counts()["test_case"], 7)

        second = load_import(self.config, path=str(DEMO_SPEC))
        stats2 = apply_import(self.repo, second, mode="merge")
        self.assertEqual(stats2.created, 0)
        self.assertEqual(stats2.updated, 0)
        self.assertEqual(stats2.cases_created, 0)
        self.assertEqual(self.repo.counts()["test_case"], 7)

    def test_merge_updates_changed_endpoint(self) -> None:
        apply_import(self.repo, load_import(self.config, path=str(DEMO_SPEC)), mode="merge")
        endpoint = self.repo.find_endpoint_by_signature("GET", "/api/health")
        self.assertIsNotNone(endpoint)
        endpoint.name = "改过的名字"
        stats = apply_import(self.repo, ImportResult(endpoints=[endpoint], cases=[], stats=ImportStats(source="manual")))
        self.assertEqual(stats.unchanged + stats.updated, 1)

    def test_replace_clears_endpoints_but_keeps_cases(self) -> None:
        apply_import(self.repo, load_import(self.config, path=str(DEMO_SPEC)), mode="merge")
        before_cases = self.repo.counts()["test_case"]
        stats = apply_import(self.repo, load_import(self.config, path=str(DEMO_SPEC)), mode="replace")
        self.assertEqual(self.repo.counts()["endpoint"], 7)
        self.assertEqual(self.repo.counts()["test_case"], before_cases)
        self.assertTrue(any("replace" in item for item in stats.warnings))

    def test_invalid_mode(self) -> None:
        with self.assertRaises(ValidationError):
            apply_import(self.repo, ImportResult(), mode="drop")

    def test_generated_case_bound_to_endpoint(self) -> None:
        apply_import(self.repo, load_import(self.config, path=str(DEMO_SPEC)), mode="merge")
        cases, _ = self.repo.list_cases(limit=100)
        bound = [case for case in cases if case.endpoint_id is not None]
        self.assertEqual(len(bound), len(cases))

    def test_quality_warnings_are_merged_into_stats(self) -> None:
        result = load_import(self.config, path=str(SWAGGER2_SPEC))
        stats = apply_import(self.repo, result, mode="merge")
        self.assertTrue(any("路径参数 id" in item for item in stats.warnings))


class FetchSpecTest(unittest.TestCase):
    def test_fetch_json_document(self) -> None:
        with LocalHttpServer(handler=_html_handler) as server, TempConfig() as config:
            text, url, size = fetch_spec(config, server.base_url + "/v2/api-docs")
            self.assertIn("swagger", text)
            self.assertGreater(size, 100)
            result = load_import(config, text=text, source="auto")
            self.assertEqual(len(result.endpoints), 7)

    def test_fetch_html_is_rejected(self) -> None:
        with LocalHttpServer(handler=_html_handler) as server, TempConfig() as config:
            with self.assertRaises(ValidationError) as ctx:
                fetch_spec(config, server.base_url + "/html")
            self.assertIn("HTML", ctx.exception.message)

    def test_fetch_http_error_is_reported(self) -> None:
        with LocalHttpServer(handler=_html_handler) as server, TempConfig() as config:
            with self.assertRaises(ValidationError) as ctx:
                fetch_spec(config, server.base_url + "/missing")
            self.assertIn("404", ctx.exception.message)

    def test_fetch_invalid_url(self) -> None:
        with TempConfig() as config:
            with self.assertRaises(ValidationError):
                fetch_spec(config, "not-a-url")

    def test_fetch_unreachable_host(self) -> None:
        with TempConfig() as config:
            with self.assertRaises(ValidationError):
                fetch_spec(config, "http://127.0.0.1:1/v2/api-docs")


if __name__ == "__main__":
    unittest.main()


class PathTemplateBindingTest(unittest.TestCase):
    """路径参数被替换后，用例仍要能绑定回原接口。"""

    def test_template_match(self) -> None:
        from hapitest.repository import path_template_matches

        self.assertTrue(path_template_matches("/api/users/{id}", "/api/users/1"))
        self.assertTrue(path_template_matches("/api/users/:id/tags", "/api/users/9/tags"))
        self.assertFalse(path_template_matches("/api/users/{id}", "/api/users/1/tags"))
        self.assertFalse(path_template_matches("/api/users/{id}", "/api/orders/1"))
        self.assertTrue(path_template_matches("/api/users", "/api/users"))
        self.assertFalse(path_template_matches("/api/users/{id}", "/api/users/"))

    def test_find_endpoint_for_path(self) -> None:
        with TempConfig() as config:
            registry = ProjectRegistry(config)
            registry.create(name="绑定", project_id="bind")
            repo = registry.repository("bind")
            endpoint = repo.create_endpoint(
                EndpointSpec(
                    name="查询详情",
                    method="GET",
                    path="/api/users/{id}",
                    params=[ParamSpec(name="id", location="path", required=True, type="integer", example=1)],
                )
            )
            self.assertEqual(repo.find_endpoint_for_path("GET", "/api/users/1").id, endpoint.id)
            self.assertEqual(repo.find_endpoint_for_path("GET", "/api/users/{id}").id, endpoint.id)
            self.assertIsNone(repo.find_endpoint_for_path("POST", "/api/users/1"))

    def test_imported_cases_are_bound(self) -> None:
        with TempConfig() as config:
            registry = ProjectRegistry(config)
            registry.create(name="绑定", project_id="bind2")
            repo = registry.repository("bind2")
            result = load_import(config, path=str(DEMO_SPEC))
            apply_import(repo, result, mode="merge")
            cases, _ = repo.list_cases(limit=100)
            unbound = [case.name for case in cases if case.endpoint_id is None]
            self.assertEqual(unbound, [], "所有生成用例都应绑定到接口")


class ResyncFromDifferentFileTest(unittest.TestCase):
    """反复同步同一份契约（导出成不同文件名）不应产生重复接口或用例。"""

    def test_same_contract_under_two_filenames(self) -> None:
        import shutil
        import tempfile

        with TempConfig() as config, tempfile.TemporaryDirectory() as tmp:
            registry = ProjectRegistry(config)
            registry.create(name="重复导出", project_id="resync")
            repo = registry.repository("resync")

            first_copy = Path(tmp) / "api-2026-10-10.json"
            second_copy = Path(tmp) / "api-2026-10-11.json"
            shutil.copyfile(DEMO_SPEC, first_copy)

            stats1 = apply_import(repo, load_import(config, path=str(first_copy)), mode="merge")
            self.assertEqual(stats1.created, 7)
            self.assertEqual(stats1.cases_created, 7)

            shutil.copyfile(DEMO_SPEC, second_copy)
            stats2 = apply_import(repo, load_import(config, path=str(second_copy)), mode="merge")
            self.assertEqual(stats2.created, 0, "同一份契约换文件名不应新增接口")
            self.assertEqual(stats2.cases_created, 0, "同一份契约换文件名不应新增用例")
            self.assertEqual(repo.counts()["endpoint"], 7)
            self.assertEqual(repo.counts()["test_case"], 7)

    def test_case_source_ref_is_endpoint_signature(self) -> None:
        with TempConfig() as config:
            registry = ProjectRegistry(config)
            registry.create(name="来源标记", project_id="ref")
            repo = registry.repository("ref")
            apply_import(repo, load_import(config, path=str(DEMO_SPEC)), mode="merge")
            cases, _ = repo.list_cases(limit=100)
            refs = {case.source_ref for case in cases}
            self.assertIn("GET /api/health", refs)
            self.assertIn("POST /api/users", refs)
            self.assertTrue(all(case.source_ref for case in cases))

    def test_resync_does_not_report_false_updates(self) -> None:
        """换文件名重新同步：接口定义没变就不应算成 updated（出处静默刷新）。"""
        import shutil
        import tempfile

        with TempConfig() as config, tempfile.TemporaryDirectory() as tmp:
            registry = ProjectRegistry(config)
            registry.create(name="信号", project_id="signal")
            repo = registry.repository("signal")
            first = Path(tmp) / "v1.json"
            second = Path(tmp) / "v2.json"
            shutil.copyfile(DEMO_SPEC, first)
            apply_import(repo, load_import(config, path=str(first)), mode="merge")
            shutil.copyfile(DEMO_SPEC, second)
            stats = apply_import(repo, load_import(config, path=str(second)), mode="merge")
            self.assertEqual(stats.updated, 0, "定义未变化不应报 updated")
            # 7 个接口 + 7 条用例，全部未变化
            self.assertEqual(stats.unchanged, 14)
            # 出处应被刷新为新文档
            endpoint = repo.find_endpoint_by_signature("GET", "/api/health")
            self.assertIn("v2.json", endpoint.source_ref)
