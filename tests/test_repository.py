"""项目注册表与仓储层测试（含项目隔离）。"""

from __future__ import annotations

import unittest
from pathlib import Path

from hapitest.errors import ConflictError, NotFoundError, ValidationError
from hapitest.models import (
    AssertionSpec,
    CaseSpec,
    EndpointSpec,
    EnvironmentSpec,
    RequestSpec,
    RunResult,
    SuiteSpec,
)
from hapitest.redact import MASK
from hapitest.repository import ProjectRegistry
from tests.support import TempConfig


class RegistryTest(unittest.TestCase):
    def test_create_and_list(self) -> None:
        with TempConfig() as config:
            registry = ProjectRegistry(config)
            record = registry.create(name="我的项目", project_id="my-app", base_url="http://127.0.0.1:8899")
            self.assertEqual(record.id, "my-app")
            self.assertTrue(registry.exists("my-app"))
            self.assertEqual([item.id for item in registry.list_projects()], ["my-app"])
            self.assertTrue(config.project_db_path("my-app").exists())

    def test_duplicate_conflict(self) -> None:
        with TempConfig() as config:
            registry = ProjectRegistry(config)
            registry.create(name="A", project_id="a")
            with self.assertRaises(ConflictError):
                registry.create(name="A2", project_id="a")

    def test_invalid_id_rejected(self) -> None:
        with TempConfig() as config:
            registry = ProjectRegistry(config)
            with self.assertRaises(ValidationError):
                registry.create(name="x", project_id="../etc")
            with self.assertRaises(Exception):
                registry.get("../../etc")

    def test_slugify_from_name(self) -> None:
        with TempConfig() as config:
            registry = ProjectRegistry(config)
            record = registry.create(name="Hapi Air V2 接口测试")
            self.assertEqual(record.id, "hapi-air-v2")

    def test_default_environment_created(self) -> None:
        with TempConfig() as config:
            registry = ProjectRegistry(config)
            registry.create(name="A", project_id="a", base_url="http://x")
            environment = registry.repository("a").resolve_environment(None)
            self.assertTrue(environment.is_default)
            self.assertEqual(environment.base_url, "http://x")

    def test_delete_purges_directory(self) -> None:
        with TempConfig() as config:
            registry = ProjectRegistry(config)
            registry.create(name="A", project_id="a")
            target = config.project_dir("a")
            self.assertTrue(target.exists())
            registry.delete("a")
            self.assertFalse(target.exists())
            self.assertFalse(registry.exists("a"))

    def test_project_isolation(self) -> None:
        with TempConfig() as config:
            registry = ProjectRegistry(config)
            registry.create(name="A", project_id="a")
            registry.create(name="B", project_id="b")
            repo_a = registry.repository("a")
            repo_b = registry.repository("b")
            repo_a.create_endpoint(EndpointSpec(name="接口A", method="GET", path="/api/a"))
            self.assertEqual(repo_a.counts()["endpoint"], 1)
            self.assertEqual(repo_b.counts()["endpoint"], 0)
            self.assertEqual(len(repo_b.list_endpoints()[0]), 0)
            self.assertNotEqual(
                config.project_db_path("a").resolve(), config.project_db_path("b").resolve()
            )


class EndpointRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._ctx = TempConfig()
        config = self._ctx.__enter__()
        self.registry = ProjectRegistry(config)
        self.registry.create(name="A", project_id="a")
        self.repo = self.registry.repository("a")

    def tearDown(self) -> None:
        self._ctx.__exit__(None, None, None)

    def test_crud_and_conflict(self) -> None:
        endpoint = self.repo.create_endpoint(EndpointSpec(name="查询用户", method="get", path="/api/users/"))
        self.assertEqual(endpoint.path, "/api/users")
        self.assertEqual(endpoint.method, "GET")
        with self.assertRaises(ConflictError):
            self.repo.create_endpoint(EndpointSpec(name="重复", method="GET", path="/api/users"))
        endpoint.name = "改名"
        updated = self.repo.update_endpoint(endpoint.id, endpoint)
        self.assertEqual(updated.name, "改名")
        self.repo.delete_endpoint(endpoint.id)
        with self.assertRaises(NotFoundError):
            self.repo.get_endpoint(endpoint.id)

    def test_upsert_actions(self) -> None:
        first, action = self.repo.upsert_endpoint(EndpointSpec(name="A", method="GET", path="/api/a"))
        self.assertEqual(action, "created")
        _, action = self.repo.upsert_endpoint(EndpointSpec(name="A", method="GET", path="/api/a"))
        self.assertEqual(action, "unchanged")
        _, action = self.repo.upsert_endpoint(EndpointSpec(name="A2", method="GET", path="/api/a"))
        self.assertEqual(action, "updated")

    def test_folder_summary(self) -> None:
        self.repo.create_endpoint(EndpointSpec(name="A", method="GET", path="/api/a", folder="用户", tags=["v1"]))
        self.repo.create_case(CaseSpec(name="C", folder="冒烟", request=RequestSpec(path="/api/a")))
        summary = self.repo.folder_summary()
        self.assertEqual(summary["endpoint_folders"], ["用户"])
        self.assertEqual(summary["case_folders"], ["冒烟"])
        self.assertIn("v1", summary["tags"])


class CaseRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._ctx = TempConfig()
        config = self._ctx.__enter__()
        self.registry = ProjectRegistry(config)
        self.registry.create(name="A", project_id="a")
        self.repo = self.registry.repository("a")

    def tearDown(self) -> None:
        self._ctx.__exit__(None, None, None)

    def test_validation_blocks_empty_name(self) -> None:
        with self.assertRaises(ValidationError):
            self.repo.create_case(CaseSpec(name="", request=RequestSpec(path="/x")))

    def test_delete_case_removes_from_suite(self) -> None:
        case = self.repo.create_case(CaseSpec(name="C1", request=RequestSpec(path="/x")))
        case2 = self.repo.create_case(CaseSpec(name="C2", request=RequestSpec(path="/y")))
        suite = self.repo.create_suite(SuiteSpec(name="S", case_ids=[case.id, case2.id]))
        self.repo.delete_case(case.id)
        self.assertEqual(self.repo.get_suite(suite.id).case_ids, [case2.id])

    def test_environment_mask_keeps_original(self) -> None:
        environment = self.repo.create_environment(
            EnvironmentSpec(name="测试", base_url="http://x", variables={"token": "secret-value"}, is_default=True)
        )
        environment.variables = {"token": MASK, "host": "127.0.0.1"}
        saved = self.repo.update_environment(environment.id, environment)
        self.assertEqual(saved.variables["token"], "secret-value")
        self.assertEqual(saved.variables["host"], "127.0.0.1")

    def test_default_environment_unique(self) -> None:
        first = self.repo.create_environment(EnvironmentSpec(name="E1", is_default=True))
        second = self.repo.create_environment(EnvironmentSpec(name="E2", is_default=True))
        self.assertTrue(self.repo.get_environment(second.id).is_default)
        self.assertFalse(self.repo.get_environment(first.id).is_default)
        self.repo.set_default_environment(first.id)
        self.assertTrue(self.repo.get_environment(first.id).is_default)


class RunRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._ctx = TempConfig()
        config = self._ctx.__enter__()
        self.registry = ProjectRegistry(config)
        self.registry.create(name="A", project_id="a")
        self.repo = self.registry.repository("a")

    def tearDown(self) -> None:
        self._ctx.__exit__(None, None, None)

    def test_run_lifecycle_and_request_id(self) -> None:
        from hapitest.models import CaseResult

        run = RunResult(project_id="a", status="queued", trigger="api", request_id="req-1", started_at="2026-01-01T00:00:00Z")
        self.repo.create_run(run)
        found = self.repo.find_run_by_request_id("req-1")
        self.assertIsNotNone(found)
        self.assertEqual(found.id, run.id)
        self.assertIsNone(self.repo.find_run_by_request_id("req-2"))

        result = CaseResult(case_name="用例A", status="passed", duration_ms=12)
        self.repo.save_case_result(run.id, result)
        run.case_results = [result]
        run.status = "passed"
        run.finished_at = "2026-01-01T00:00:01Z"
        run.duration_ms = 1000
        self.repo.update_run(run)

        loaded = self.repo.get_run(run.id, with_cases=True)
        self.assertEqual(loaded.status, "passed")
        self.assertEqual(loaded.total, 1)
        self.assertEqual(loaded.passed, 1)
        self.assertEqual(len(loaded.case_results), 1)
        self.assertEqual(loaded.case_results[0].case_name, "用例A")

        stats = self.repo.stats()
        self.assertEqual(stats["counts"]["run"], 1)
        self.assertEqual(stats["pass_rate"], 1.0)

    def test_fingerprint_changes_with_definition(self) -> None:
        case = self.repo.create_case(
            CaseSpec(
                name="C",
                request=RequestSpec(path="/x"),
                assertions=[AssertionSpec(type="status_code", operator="eq", expected=200)],
            )
        )
        first = self.repo.fingerprint([case.id])
        case.assertions = [AssertionSpec(type="status_code", operator="eq", expected=404)]
        self.repo.update_case(case.id, case)
        second = self.repo.fingerprint([case.id])
        self.assertNotEqual(first, second)

    def test_import_batch_recorded(self) -> None:
        from hapitest.models import ImportStats

        stats = ImportStats(source="openapi", source_ref="demo.json", created=3)
        batch_id = self.repo.record_import(stats, title="演示")
        batches = self.repo.list_imports()
        self.assertEqual(batches[0]["id"], batch_id)
        self.assertEqual(batches[0]["stats"]["created"], 3)


if __name__ == "__main__":
    unittest.main()
