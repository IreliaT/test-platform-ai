"""项目注册表与仓储层测试（含项目隔离与结构迁移）。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from hapitest import db
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
        case = self.repo.create_case(CaseSpec(name="C2", request=RequestSpec(path="/api/a")))
        self.repo.create_suite(SuiteSpec(name="S", folder="回归", case_ids=[case.id]))
        summary = self.repo.folder_summary()
        self.assertEqual(summary["endpoint_folders"], ["用户"])
        self.assertEqual(summary["case_folders"], ["冒烟"])
        # 套件目录独立统计，供套件页的「目录」筛选使用
        self.assertEqual(summary["suite_folders"], ["回归"])
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

    def test_suite_folder_round_trip(self) -> None:
        case = self.repo.create_case(CaseSpec(name="C1", request=RequestSpec(path="/x")))
        suite = self.repo.create_suite(
            SuiteSpec(name="A2 分页列表", folder="官方靓号库", case_ids=[case.id])
        )
        self.assertEqual(self.repo.get_suite(suite.id).folder, "官方靓号库")
        self.assertEqual(suite.to_dict()["folder"], "官方靓号库")

        suite.folder = "官方靓号库/后台"
        self.repo.update_suite(suite.id, suite)
        reloaded = self.repo.get_suite(suite.id)
        self.assertEqual(reloaded.folder, "官方靓号库/后台")

        # 未指定目录时落「未分组」，不应出现 None
        plain = self.repo.create_suite(SuiteSpec(name="无目录", case_ids=[case.id]))
        self.assertEqual(self.repo.get_suite(plain.id).folder, "")

    def test_suite_variables_round_trip(self) -> None:
        case = self.repo.create_case(CaseSpec(name="C1", request=RequestSpec(path="/x")))
        suite = self.repo.create_suite(
            SuiteSpec(name="带参数的套件", variables={"uid": "11002850"}, case_ids=[case.id])
        )
        self.assertEqual(self.repo.get_suite(suite.id).variables, {"uid": "11002850"})

        suite.variables = {"uid": "2", "reason": "回归"}
        self.repo.update_suite(suite.id, suite)
        self.assertEqual(self.repo.get_suite(suite.id).variables, {"uid": "2", "reason": "回归"})

        plain = self.repo.create_suite(SuiteSpec(name="无参数", case_ids=[case.id]))
        self.assertEqual(self.repo.get_suite(plain.id).variables, {})

    def test_suite_folder_trims_whitespace(self) -> None:
        parsed = SuiteSpec.from_dict({"name": "S", "folder": "  官方靓号库  "})
        self.assertEqual(parsed.folder, "官方靓号库")

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


class SchemaMigrationTest(unittest.TestCase):
    """v1 老库升级到 v2：套件补 folder 列，历史数据落「未分组」，且可重复执行。"""

    V1_SQL = """
    CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    INSERT INTO schema_meta(key, value) VALUES ('schema_version', '1');
    CREATE TABLE suite (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        case_ids_json TEXT NOT NULL DEFAULT '[]',
        env_id INTEGER,
        failure_strategy TEXT NOT NULL DEFAULT 'continue',
        concurrency INTEGER NOT NULL DEFAULT 1,
        tags_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT ''
    );
    INSERT INTO suite (name, description, case_ids_json) VALUES ('老套件', '迁移前数据', '[1,2]');
    """

    def test_v1_database_gains_folder_and_keeps_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "platform.db"
            conn = sqlite3.connect(str(path))
            conn.executescript(self.V1_SQL)
            conn.commit()
            conn.close()

            upgraded = db.connect(path)
            columns = {row[1] for row in upgraded.execute("PRAGMA table_info(suite)")}
            self.assertIn("folder", columns)
            self.assertIn("variables_json", columns)
            row = upgraded.execute("SELECT * FROM suite").fetchone()
            self.assertEqual(row["name"], "老套件")
            self.assertEqual(row["folder"], "")
            self.assertEqual(db.get_meta(upgraded, "schema_version"), str(db.SCHEMA_VERSION))
            upgraded.close()

            # 幂等：再连一次不报错、不重复加列
            again = db.connect(path)
            self.assertEqual(
                [r[1] for r in again.execute("PRAGMA table_info(suite)")].count("folder"), 1
            )
            self.assertEqual(again.execute("SELECT COUNT(*) FROM suite").fetchone()[0], 1)
            again.close()

    def test_new_database_has_folder_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "platform.db"
            conn = db.connect(path)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(suite)")}
            self.assertIn("folder", columns)
            self.assertIn("variables_json", columns)
            self.assertEqual(db.get_meta(conn, "schema_version"), str(db.SCHEMA_VERSION))
            conn.close()


if __name__ == "__main__":
    unittest.main()
