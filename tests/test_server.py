"""HTTP API 测试：信封、CRUD、运行、报告、错误码。"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from hapitest.server.app import create_server
from tests.support import LocalHttpServer, TempConfig


class ApiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._target_ctx = LocalHttpServer()
        cls.target = cls._target_ctx.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._target_ctx.__exit__(None, None, None)

    def setUp(self) -> None:
        self._ctx = TempConfig()
        self.config = self._ctx.__enter__()
        self.httpd, self.app = create_server(self.config, host="127.0.0.1", port=0)
        self.app.runs.start()
        self.port = int(self.httpd.server_address[1])
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.app.runs.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.app.registry.close()
        self._ctx.__exit__(None, None, None)

    # --- 辅助 ---
    def call(self, method: str, path: str, body=None, expect_status: int = 200):
        # 路径里可能包含中文查询参数，必须做百分号编码
        safe_path = urllib.parse.quote(path, safe="/?&=%:")
        url = "http://127.0.0.1:%d%s" % (self.port, safe_path)
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                status = response.status
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
            content_type = exc.headers.get("Content-Type", "")
        self.assertEqual(status, expect_status, "HTTP %s: %s" % (status, raw[:500]))
        if content_type.startswith("application/json"):
            return json.loads(raw.decode("utf-8"))
        return raw.decode("utf-8")

    def data_of(self, response):
        self.assertTrue(response["ok"], response)
        return response["data"]

    # --- 用例 ---
    def test_health_and_meta(self) -> None:
        health = self.data_of(self.call("GET", "/api/v1/health"))
        self.assertEqual(health["name"], "HapiTest")
        self.assertIn("version", health)
        meta = self.data_of(self.call("GET", "/api/v1/meta"))
        self.assertIn("status_code", meta["assertion_types"])
        self.assertIn("passed", meta["case_statuses"])
        self.assertIn("incomplete", meta["case_statuses"])

    def test_error_envelope_and_status(self) -> None:
        response = self.call("GET", "/api/v1/projects/not-exist", expect_status=404)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "not_found")
        response = self.call("GET", "/api/v1/no-such-route", expect_status=404)
        self.assertFalse(response["ok"])
        response = self.call("DELETE", "/api/v1/health", expect_status=405)
        self.assertEqual(response["error"]["code"], "method_not_allowed")

    def test_static_index(self) -> None:
        html = self.call("GET", "/")
        self.assertIn("<html", html.lower())
        # 入口脚本带资源版本号，避免改了前端还加载浏览器缓存的旧模块
        self.assertRegex(html, r'src="/assets/app\.js\?v=\d+"')
        html = self.call("GET", "/some/spa/route")
        self.assertIn("<html", html.lower())

    def test_project_lifecycle(self) -> None:
        created = self.data_of(self.call("POST", "/api/v1/projects", {"name": "接口测试", "base_url": self.target.base_url}))
        project = created["project"]
        # 纯中文名称无法生成 ASCII slug，按约定回退为 project-<名称哈希>
        self.assertRegex(project["id"], r"^project-[0-9a-f]{6}$")
        self.assertEqual(created["counts"]["endpoint"], 0)

        listed = self.data_of(self.call("GET", "/api/v1/projects"))
        self.assertEqual(listed["total"], 1)

        detail = self.data_of(self.call("GET", "/api/v1/projects/%s" % project["id"]))
        self.assertEqual(detail["project"]["name"], "接口测试")

        updated = self.data_of(self.call("PUT", "/api/v1/projects/%s" % project["id"], {"description": "备注"}))
        self.assertEqual(updated["project"]["description"], "备注")

        response = self.call("DELETE", "/api/v1/projects/%s" % project["id"], expect_status=400)
        self.assertEqual(response["error"]["code"], "bad_request")
        deleted = self.data_of(self.call("DELETE", "/api/v1/projects/%s?confirm=%s" % (project["id"], project["id"])))
        self.assertTrue(deleted["deleted"])
        self.call("GET", "/api/v1/projects/%s" % project["id"], expect_status=404)

    def test_endpoint_case_run_flow(self) -> None:
        project = self.data_of(self.call("POST", "/api/v1/projects", {"name": "流动测试", "base_url": self.target.base_url}))["project"]
        pid = project["id"]

        endpoint = self.data_of(
            self.call(
                "POST",
                "/api/v1/projects/%s/endpoints" % pid,
                {"name": "健康检查", "method": "GET", "path": "/ok", "folder": "健康", "tags": ["v1"]},
            )
        )["endpoint"]
        self.assertEqual(endpoint["path"], "/ok")

        listing = self.data_of(self.call("GET", "/api/v1/projects/%s/endpoints?q=健康" % pid))
        self.assertEqual(listing["total"], 1)

        probe = self.data_of(self.call("POST", "/api/v1/projects/%s/endpoints/%d/probe" % (pid, endpoint["id"]), {}))
        self.assertEqual(probe["response"]["status"], 200)

        case = self.data_of(
            self.call("POST", "/api/v1/projects/%s/endpoints/%d/to-case" % (pid, endpoint["id"]), {"name": "健康检查-冒烟"})
        )["case"]
        self.assertEqual(case["endpoint_id"], endpoint["id"])
        self.assertTrue(case["assertions"])

        folders = self.data_of(self.call("GET", "/api/v1/projects/%s/folders" % pid))
        self.assertIn("健康", folders["endpoint_folders"])

        suite = self.data_of(
            self.call("POST", "/api/v1/projects/%s/suites" % pid, {"name": "冒烟套件", "case_ids": [case["id"]]})
        )["suite"]
        self.assertEqual(suite["case_ids"], [case["id"]])

        run_response = self.data_of(
            self.call("POST", "/api/v1/projects/%s/runs" % pid, {"suite_id": suite["id"], "background": False, "request_id": "api-1"})
        )
        run = run_response["run"]
        self.assertEqual(run["status"], "passed", run)
        self.assertEqual(run["passed"], 1)
        self.assertEqual(run["incomplete"], 0)

        detail = self.data_of(self.call("GET", "/api/v1/projects/%s/runs/%s" % (pid, run["id"])))["run"]
        self.assertEqual(len(detail["case_results"]), 1)
        self.assertEqual(detail["case_results"][0]["status"], "passed")
        self.assertTrue(detail["case_results"][0]["request"]["url"].endswith("/ok"))
        self.assertEqual(detail["case_results"][0]["response"]["status"], 200)

        # 幂等：同 request_id 返回同一个运行
        again = self.data_of(
            self.call("POST", "/api/v1/projects/%s/runs" % pid, {"suite_id": suite["id"], "background": False, "request_id": "api-1"})
        )
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["run"]["id"], run["id"])

        xml_text = self.call("GET", "/api/v1/projects/%s/runs/%s/report?format=xml" % (pid, run["id"]))
        root = ET.fromstring(xml_text)
        self.assertEqual(root.tag, "testsuites")
        self.assertEqual(len(root.findall(".//testcase")), 1)

        html = self.call("GET", "/api/v1/projects/%s/runs/%s/report?format=html" % (pid, run["id"]))
        self.assertIn("<html", html.lower())

        stats = self.data_of(self.call("GET", "/api/v1/projects/%s/stats" % pid))
        self.assertEqual(stats["counts"]["run"], 1)
        self.assertEqual(stats["pass_rate"], 1.0)
        self.assertEqual(len(stats["trend"]), 1)

        single = self.data_of(self.call("POST", "/api/v1/projects/%s/cases/%d/run" % (pid, case["id"]), {}))
        self.assertEqual(single["result"]["status"], "passed")

        duplicated = self.data_of(self.call("POST", "/api/v1/projects/%s/cases/%d/duplicate" % (pid, case["id"])))
        self.assertIn("副本", duplicated["case"]["name"])

        deleted = self.data_of(self.call("DELETE", "/api/v1/projects/%s/cases/%d" % (pid, case["id"])))
        self.assertTrue(deleted["deleted"])

        exported = self.data_of(self.call("GET", "/api/v1/projects/%s/export" % pid))
        self.assertIn("endpoints", exported)


    def test_single_case_run_uses_temporary_variables_with_history(self) -> None:
        project = self.data_of(self.call("POST", "/api/v1/projects", {
            "name": "运行参数", "base_url": self.target.base_url,
        }))["project"]
        pid = project["id"]
        env = self.data_of(self.call("POST", "/api/v1/projects/%s/environments" % pid, {
            "name": "本地", "is_default": True,
            "variables": {"value": "environment", "inherited_value": "keep", "user_token": "env-token"},
        }))["environment"]
        case = self.data_of(self.call("POST", "/api/v1/projects/%s/cases" % pid, {
            "name": "临时参数", "variables": {"value": "case"},
            "setup": [{"name": "检查参数", "required_variables": ["user_token", "value"],
                       "request": {"method": "GET", "path": "/ok"},
                       "assertions": [{"type": "variable", "name": "value", "expected": "override"}]}],
            "request": {"method": "GET", "path": "/echo",
                        "headers": [{"name": "Authorization", "value": "Bearer ${user_token}"}],
                        "query": [{"name": "value", "value": "${value}"},
                                  {"name": "inherited", "value": "${inherited_value}"}]},
            "assertions": [{"type": "status_code", "expected": 200}],
        }))["case"]
        for persist in (True, False):
            result = self.data_of(self.call("POST", "/api/v1/projects/%s/cases/%d/run" % (pid, case["id"]), {
                "env_id": env["id"], "persist": persist,
                "variables": {"value": "override", "user_token": "temporary-test-token"},
            }))
            self.assertEqual(result["result"]["status"], "passed", result)
            self.assertIn("value=override", result["result"]["request"]["url"])
            self.assertIn("inherited=keep", result["result"]["request"]["url"])
            self.assertEqual(self.target.records[-1]["headers"]["Authorization"], "Bearer temporary-test-token")
            self.assertNotIn("temporary-test-token", json.dumps(result))
            if persist:
                run_id = result["run"]["id"]
                snapshot = self.config.project_dir(pid) / "runs" / (run_id + ".json")
                self.assertNotIn("temporary-test-token", snapshot.read_text())
                self.assertEqual(json.loads(snapshot.read_text())["variables"]["user_token"], "******")
                history = self.data_of(self.call("GET", "/api/v1/projects/%s/runs/%s" % (pid, run_id)))
                self.assertEqual(history["run"]["case_results"][0]["status"], "passed")
                self.assertNotIn("temporary-test-token", json.dumps(history))
            else:
                self.assertNotIn("run", result)
        self.assertEqual(self.app.registry.repository(pid).get_environment(env["id"]).variables["user_token"], "env-token")
        self.assertEqual(self.app.registry.repository(pid).get_case(case["id"]).variables, {"value": "case"})

    def test_environment_switch_changes_what_runs_use(self) -> None:
        """右上角切换环境 = 改默认环境；不带 env_id 的运行必须跟着走，显式指定仍然优先。"""
        project = self.data_of(self.call("POST", "/api/v1/projects", {"name": "环境切换", "base_url": ""}))["project"]
        pid = project["id"]

        # 环境 A 指向可用的本地服务；环境 B 指向不可达端口
        env_a = self.data_of(
            self.call(
                "POST",
                "/api/v1/projects/%s/environments" % pid,
                {"name": "可用环境", "base_url": self.target.base_url, "is_default": True},
            )
        )["environment"]
        env_b = self.data_of(
            self.call(
                "POST",
                "/api/v1/projects/%s/environments" % pid,
                {"name": "不可达环境", "base_url": "http://127.0.0.1:1"},
            )
        )["environment"]

        case = self.data_of(
            self.call(
                "POST",
                "/api/v1/projects/%s/cases" % pid,
                {
                    "name": "环境切换用例",
                    "request": {"method": "GET", "path": "/ok"},
                    "assertions": [{"type": "status_code", "operator": "eq", "expected": 200}],
                },
            )
        )["case"]

        # 1) A 是默认环境：不传 env_id 应当走 A
        run1 = self.data_of(
            self.call("POST", "/api/v1/projects/%s/runs" % pid, {"case_ids": [case["id"]], "background": False})
        )["run"]
        self.assertEqual(run1["status"], "passed", run1)
        self.assertEqual(run1["env_id"], env_a["id"])
        self.assertEqual(run1["env_name"], "可用环境")

        # 2) 切换默认环境（等价于界面右上角切换）→ 同一个用例改为走 B
        switched = self.data_of(self.call("POST", "/api/v1/projects/%s/environments/%d/default" % (pid, env_b["id"])))
        self.assertTrue(switched["environment"]["is_default"])

        run2 = self.data_of(
            self.call("POST", "/api/v1/projects/%s/runs" % pid, {"case_ids": [case["id"]], "background": False})
        )["run"]
        self.assertEqual(run2["env_id"], env_b["id"])
        self.assertEqual(run2["env_name"], "不可达环境")
        self.assertEqual(run2["status"], "error", "切到不可达环境后应判为 error，而不是沿用旧环境")

        # 3) 显式 env_id 仍然优先于默认环境（CI 必须可复现）
        run3 = self.data_of(
            self.call(
                "POST",
                "/api/v1/projects/%s/runs" % pid,
                {"case_ids": [case["id"]], "env_id": env_a["id"], "background": False},
            )
        )["run"]
        self.assertEqual(run3["status"], "passed", run3)
        self.assertEqual(run3["env_id"], env_a["id"])

    def test_environment_switch_carries_variables(self) -> None:
        """切换环境不只是换地址，变量也要跟着换（例如 ${token}）。"""
        project = self.data_of(self.call("POST", "/api/v1/projects", {"name": "环境变量", "base_url": ""}))["project"]
        pid = project["id"]
        self.data_of(
            self.call(
                "POST",
                "/api/v1/projects/%s/environments" % pid,
                {"name": "A", "base_url": self.target.base_url, "variables": {"who": "aaa"}, "is_default": True},
            )
        )
        env_b = self.data_of(
            self.call(
                "POST",
                "/api/v1/projects/%s/environments" % pid,
                {"name": "B", "base_url": self.target.base_url, "variables": {"who": "bbb"}},
            )
        )["environment"]
        case = self.data_of(
            self.call(
                "POST",
                "/api/v1/projects/%s/cases" % pid,
                {
                    "name": "变量用例",
                    "request": {
                        "method": "GET",
                        "path": "/echo",
                        "headers": [{"name": "X-Who", "value": "${who}"}],
                    },
                    "assertions": [{"type": "body_text", "operator": "contains", "expected": "aaa"}],
                },
            )
        )["case"]

        run1 = self.data_of(
            self.call("POST", "/api/v1/projects/%s/runs" % pid, {"case_ids": [case["id"]], "background": False})
        )["run"]
        self.assertEqual(run1["status"], "passed", run1)

        self.data_of(self.call("POST", "/api/v1/projects/%s/environments/%d/default" % (pid, env_b["id"])))
        case["assertions"] = [{"type": "body_text", "operator": "contains", "expected": "bbb"}]
        self.data_of(self.call("PUT", "/api/v1/projects/%s/cases/%d" % (pid, case["id"]), case))
        run2 = self.data_of(
            self.call("POST", "/api/v1/projects/%s/runs" % pid, {"case_ids": [case["id"]], "background": False})
        )["run"]
        self.assertEqual(run2["status"], "passed", run2)

    def test_case_validation_error_details(self) -> None:
        project = self.data_of(self.call("POST", "/api/v1/projects", {"name": "校验测试"}))["project"]
        response = self.call(
            "POST",
            "/api/v1/projects/%s/cases" % project["id"],
            {"name": "", "request": {"path": "/x"}},
            expect_status=400,
        )
        self.assertFalse(response["ok"])
        self.assertTrue(response["error"]["details"])

    def test_environment_masking_round_trip(self) -> None:
        project = self.data_of(self.call("POST", "/api/v1/projects", {"name": "掩码测试"}))["project"]
        pid = project["id"]
        environment = self.data_of(
            self.call(
                "POST",
                "/api/v1/projects/%s/environments" % pid,
                {"name": "测试环境", "base_url": "http://127.0.0.1:1", "variables": {"token": "real-secret", "host": "h"}},
            )
        )["environment"]
        self.assertEqual(environment["variables"]["token"], "******")
        environment["variables"] = {"token": "******", "host": "h2"}
        saved = self.data_of(
            self.call("PUT", "/api/v1/projects/%s/environments/%d" % (pid, environment["id"]), environment)
        )["environment"]
        self.assertEqual(saved["variables"]["token"], "******")
        from hapitest.repository import ProjectRegistry

        raw = ProjectRegistry(self.config).repository(pid).get_environment(environment["id"])
        self.assertEqual(raw.variables["token"], "real-secret")
        self.assertEqual(raw.variables["host"], "h2")

    def test_run_diff_endpoint(self) -> None:
        project = self.data_of(self.call("POST", "/api/v1/projects", {"name": "对比测试", "base_url": self.target.base_url}))["project"]
        pid = project["id"]
        case = self.data_of(
            self.call(
                "POST",
                "/api/v1/projects/%s/cases" % pid,
                {
                    "name": "对比用例",
                    "request": {"method": "GET", "path": "/ok"},
                    "assertions": [{"type": "status_code", "operator": "eq", "expected": 200}],
                },
            )
        )["case"]
        first = self.data_of(
            self.call("POST", "/api/v1/projects/%s/runs" % pid, {"case_ids": [case["id"]], "background": False})
        )["run"]
        case["assertions"] = [{"type": "status_code", "operator": "eq", "expected": 500}]
        self.data_of(self.call("PUT", "/api/v1/projects/%s/cases/%d" % (pid, case["id"]), case))
        second = self.data_of(
            self.call("POST", "/api/v1/projects/%s/runs" % pid, {"case_ids": [case["id"]], "background": False})
        )["run"]
        self.assertEqual(second["status"], "failed")

        diff = self.data_of(self.call("GET", "/api/v1/projects/%s/runs/%s/diff?base=%s" % (pid, second["id"], first["id"])))
        self.assertEqual(diff["summary"]["newly_failed"], 1)
        self.assertEqual(diff["changes"][0]["kind"], "status_changed")

    def test_invalid_json_body(self) -> None:
        url = "http://127.0.0.1:%d/api/v1/projects" % self.port
        request = urllib.request.Request(
            url, data=b"{not json", headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            urllib.request.urlopen(request, timeout=10)
            self.fail("应当返回 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
            payload = json.loads(exc.read().decode("utf-8"))
            self.assertEqual(payload["error"]["code"], "bad_request")


if __name__ == "__main__":
    unittest.main()
