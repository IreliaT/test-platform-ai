"""MCP 导入通道的测试（使用本地假 MCP 服务，不联网、不依赖 Node）。

覆盖：握手、按前缀匹配工具名、令牌掩码、超时、服务提前退出、刷新开关，
以及"通过 MCP 取回文档并解析成接口"的完整链路。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from hapitest.errors import ValidationError
from hapitest.mcp_client import (
    TOOL_READ_OAS,
    TOOL_REFRESH_OAS,
    McpStdioClient,
    build_apifox_command,
    fetch_apifox_openapi,
    mask_secrets,
)
from hapitest.repository import ProjectRegistry
from hapitest.sync import apifox_token, apply_import, load_import_from_mcp
from tests.support import TempConfig

FAKE_SERVER = str(Path(__file__).resolve().parent / "fake_mcp_server.py")
TOKEN = "secret-token-12345"
TOKEN_ENV = "APIFOX_ACCESS_TOKEN"


def fake_command(mode: str = "ok") -> list:
    """假 MCP 服务命令；模式通过 argv 传入，测试里不依赖环境变量与打桩。"""
    return [sys.executable, FAKE_SERVER, "--mode=%s" % mode]


def fake_env() -> dict:
    return {TOKEN_ENV: TOKEN}


class CommandBuildTest(unittest.TestCase):
    def test_default_command_with_project(self) -> None:
        command = build_apifox_command(project_id="123456")
        self.assertEqual(command[:3], ["npx", "-y", "apifox-mcp-server@latest"])
        self.assertIn("--project=123456", command)

    def test_private_deployment_base_url(self) -> None:
        command = build_apifox_command(project_id="1", api_base_url="https://apifox.internal")
        self.assertIn("--apifox-api-base-url=https://apifox.internal", command)

    def test_requires_some_target(self) -> None:
        with self.assertRaises(ValidationError):
            build_apifox_command()

    def test_mask_secrets(self) -> None:
        self.assertEqual(mask_secrets("token=%s boom" % TOKEN, [TOKEN]), "token=****** boom")
        self.assertEqual(mask_secrets("short", ["ab"]), "short")  # 过短的值不掩码，避免误伤


class McpClientTest(unittest.TestCase):
    def test_handshake_and_prefix_tool_lookup(self) -> None:
        with McpStdioClient(fake_command(), env=fake_env(), timeout_s=30) as client:
            client.initialize()
            self.assertEqual(client.server_info.get("name"), "Fake Apifox MCP")
            tools = client.list_tools()
            self.assertEqual(len(tools), 3)
            # 工具名带随机后缀，必须按前缀匹配
            self.assertEqual(client.find_tool(TOOL_READ_OAS), "read_project_oas_abc123")
            self.assertEqual(client.find_tool(TOOL_REFRESH_OAS), "refresh_project_oas_abc123")

    def test_call_tool_returns_text(self) -> None:
        with McpStdioClient(fake_command(), env=fake_env(), timeout_s=30) as client:
            client.initialize()
            client.list_tools()
            result = client.call_tool(client.find_tool(TOOL_READ_OAS), {})
            self.assertTrue(result.ok)
            self.assertIn('"openapi"', result.text)

    def test_tool_error_is_reported_and_token_masked(self) -> None:
        with McpStdioClient(fake_command("error"), env=fake_env(), timeout_s=30, secrets=[TOKEN]) as client:
            client.initialize()
            client.list_tools()
            result = client.call_tool(client.find_tool(TOOL_READ_OAS), {})
            self.assertFalse(result.ok)
            self.assertTrue(result.is_error)
            # 工具返回值本身不改写，掩码由上层错误信息负责
            self.assertIn(TOKEN, result.text)

    def test_service_exit_is_reported_with_stderr_hint(self) -> None:
        client = McpStdioClient(fake_command("exit"), env=fake_env(), timeout_s=10)
        client.start()
        try:
            with self.assertRaises(ValidationError) as ctx:
                client.initialize()
            message = ctx.exception.message
            self.assertTrue("提前退出" in message or "MCP" in message)
            self.assertIn("Please provide a project id", message)
        finally:
            client.close()

    def test_no_tools(self) -> None:
        with McpStdioClient(fake_command("no_tools"), env=fake_env(), timeout_s=15) as client:
            client.initialize()
            self.assertEqual(client.list_tools(), [])
            self.assertIsNone(client.find_tool(TOOL_READ_OAS))

    def test_timeout(self) -> None:
        with McpStdioClient(fake_command("slow"), env=fake_env(), timeout_s=1.5) as client:
            client.initialize()
            client.list_tools()
            with self.assertRaises(ValidationError) as ctx:
                client.call_tool(client.find_tool(TOOL_READ_OAS), {}, timeout_s=1.0)
            self.assertIn("超时", ctx.exception.message)

    def test_missing_executable(self) -> None:
        client = McpStdioClient(["definitely-not-a-real-binary-xyz"], env=fake_env(), timeout_s=5)
        with self.assertRaises(ValidationError) as ctx:
            client.start()
        self.assertIn("找不到命令", ctx.exception.message)


class FetchApifoxOpenApiTest(unittest.TestCase):
    def test_end_to_end_fetch(self) -> None:
        warnings: list = []
        text, meta = fetch_apifox_openapi(
            project_id="123456",
            token=TOKEN,
            command=fake_command(),
            timeout_s=30,
            warnings=warnings,
        )
        self.assertIn('"openapi"', text)
        self.assertEqual(meta["refreshed"], True)
        self.assertEqual(meta["tool"], "read_project_oas_abc123")
        self.assertEqual(meta["server"], "Fake Apifox MCP 0.0.0")
        self.assertGreater(meta["bytes"], 100)

    def test_refresh_can_be_disabled(self) -> None:
        _, meta = fetch_apifox_openapi(
            project_id="1", token=TOKEN, command=fake_command(), refresh=False, timeout_s=30
        )
        self.assertFalse(meta["refreshed"])

    def test_read_failure_reports_hint_without_token(self) -> None:
        """工具报错时，错误信息必须掩码令牌并给出可操作提示。"""
        with self.assertRaises(ValidationError) as ctx:
            fetch_apifox_openapi(project_id="1", token=TOKEN, command=fake_command("error"), timeout_s=20)
        message = ctx.exception.message
        self.assertNotIn(TOKEN, message)
        self.assertIn("******", message)
        self.assertIn("访问令牌", message)

    def test_missing_token(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            fetch_apifox_openapi(project_id="1", token=None, command=fake_command())
        self.assertIn("令牌", ctx.exception.message)

    def test_no_read_tool(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            fetch_apifox_openapi(project_id="1", token=TOKEN, command=fake_command("no_tools"), timeout_s=20)
        self.assertIn("未提供接口文档读取工具", ctx.exception.message)

    def test_garbage_content_is_rejected_by_parser(self) -> None:
        """能取回文本但不含任何接口：由解析层报错，而不是当成"空项目"静默通过。"""
        with TempConfig() as config:
            with self.assertRaises(ValidationError) as ctx:
                load_import_from_mcp(
                    config, project_id="1", token=TOKEN, command=fake_command("garbage"), timeout_s=20
                )
            self.assertIn("无法解析", ctx.exception.message)


class TokenResolutionTest(unittest.TestCase):
    def test_env_file_is_used_when_process_env_missing(self) -> None:
        import os

        saved = os.environ.pop(TOKEN_ENV, None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / ".env.local").write_text("%s=from-env-file\n" % TOKEN_ENV, encoding="utf-8")
                self.assertEqual(apifox_token(root, TOKEN_ENV), "from-env-file")
            with tempfile.TemporaryDirectory() as tmp:
                self.assertIsNone(apifox_token(Path(tmp), TOKEN_ENV))
        finally:
            if saved:
                os.environ[TOKEN_ENV] = saved


class LoadImportFromMcpTest(unittest.TestCase):
    def test_parse_into_endpoints_and_cases(self) -> None:
        with TempConfig() as config:
            registry = ProjectRegistry(config)
            registry.create(name="MCP 同步", project_id="mcp")
            repo = registry.repository("mcp")

            result, meta = load_import_from_mcp(
                config, project_id="123456", token=TOKEN, command=fake_command(), timeout_s=30
            )
            self.assertEqual(result.source, "apifox")
            self.assertEqual(len(result.endpoints), 7)
            self.assertEqual(len(result.cases), 7)
            self.assertTrue(any(str(w).startswith("detected_server_url") for w in result.stats.warnings))
            self.assertEqual(meta["parsed_endpoints"], 7)

            stats = apply_import(repo, result, mode="merge")
            self.assertEqual(stats.created, 7)
            self.assertEqual(stats.cases_created, 7)

            # 用例 source_ref 规范为接口签名，重复同步不会重复建用例
            again, _ = load_import_from_mcp(
                config, project_id="123456", token=TOKEN, command=fake_command(), timeout_s=30
            )
            stats2 = apply_import(repo, again, mode="merge")
            self.assertEqual(stats2.created, 0)
            self.assertEqual(stats2.cases_created, 0)
            self.assertEqual(repo.counts()["test_case"], 7)

    def test_unparsable_content(self) -> None:
        with TempConfig() as config:
            with self.assertRaises(ValidationError) as ctx:
                load_import_from_mcp(
                    config, project_id="x", token=TOKEN, command=[sys.executable, "-c", "print('nope')"], timeout_s=5
                )
            self.assertTrue("MCP" in ctx.exception.message or "无法解析" in ctx.exception.message)


if __name__ == "__main__":
    unittest.main()


class BranchSelectionTest(unittest.TestCase):
    """主分支/迭代分支的选择必须显式可配（否则会误判"项目里只有几条接口"）。"""

    def test_branch_id_is_passed_to_service(self) -> None:
        command = build_apifox_command(project_id="8882175", branch_id="123456")
        self.assertIn("--project=8882175", command)
        self.assertIn("--branch-id=123456", command)

    def test_without_branch_reads_default_branch(self) -> None:
        command = build_apifox_command(project_id="8882175")
        self.assertFalse(any(item.startswith("--branch-id") for item in command))

    def test_meta_records_branch(self) -> None:
        _, meta = fetch_apifox_openapi(
            project_id="1", token=TOKEN, command=fake_command(), timeout_s=30, branch_id="999"
        )
        self.assertEqual(meta["branch_id"], "999")
        _, meta2 = fetch_apifox_openapi(project_id="1", token=TOKEN, command=fake_command(), timeout_s=30)
        self.assertEqual(meta2["branch_id"], "(主分支)")
