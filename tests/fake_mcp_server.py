#!/usr/bin/env python3
"""测试用的假 MCP 服务（stdio JSON-RPC 2.0），用于确定性地验证 MCP 导入路径。

不是产品代码：只在 `tests/test_mcp_client.py` 里被拉起。
行为由环境变量 FAKE_MCP_MODE 控制：

- `ok`（默认）：正常返回工具列表与 OpenAPI 文档；
- `error`：读取文档的工具返回 isError，且错误信息里带上令牌（用于验证掩码）；
- `slow`：收到调用后不响应（用于验证超时）；
- `no_tools`：工具列表为空；
- `garbage`：返回无法解析的文本；
- `exit`：立刻退出（模拟 npx/Node 不可用或服务崩溃）。

模式可用命令行参数指定：``--mode=error``（优先于环境变量）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

MODE = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--mode=")), os.environ.get("FAKE_MCP_MODE", "ok"))
SUFFIX = "abc123"  # 模拟 Apifox 的随机后缀，客户端必须按前缀匹配
SPEC = Path(__file__).resolve().parent.parent / "examples" / "demo_specs" / "demo_openapi.json"

TOOLS = [
    {
        "name": "read_project_oas_%s" % SUFFIX,
        "description": "读取项目的 OpenAPI Spec 文件内容",
        "inputSchema": {"type": "object", "properties": {"_": {"type": "string"}}, "additionalProperties": False},
    },
    {
        "name": "refresh_project_oas_%s" % SUFFIX,
        "description": "从服务器重新下载最新的 OpenAPI Spec 文件内容",
        "inputSchema": {"type": "object", "properties": {"_": {"type": "string"}}, "additionalProperties": False},
    },
    {
        "name": "read_project_oas_ref_resources_%s" % SUFFIX,
        "description": "读取项目 OpenAPI Spec 文件内 $ref 的文件内容",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "array", "items": {"type": "string"}}},
            "required": ["path"],
        },
    },
]


def respond(message_id, result=None, error=None) -> None:
    payload = {"jsonrpc": "2.0", "id": message_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result if result is not None else {}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def tool_text(text: str, is_error: bool = False):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def handle_tool_call(name: str, arguments: dict):
    token = os.environ.get("APIFOX_ACCESS_TOKEN", "")
    if name.startswith("refresh_project_oas"):
        return tool_text("refreshed")
    if name.startswith("read_project_oas_ref_resources"):
        return tool_text(json.dumps({"/paths/_get_pet.json": {"get": {"summary": "pet"}}}, ensure_ascii=False))
    if name.startswith("read_project_oas"):
        if MODE == "error":
            return tool_text("invalid token: %s (401 Unauthorized)" % token, is_error=True)
        if MODE == "garbage":
            return tool_text("这不是一份接口文档")
        return tool_text(SPEC.read_text(encoding="utf-8"))
    return tool_text("unknown tool: %s" % name, is_error=True)


def main() -> int:
    if MODE == "exit":
        sys.stderr.write("Please provide a project id or a doc site id or an OpenAPI Spec file\n")
        return 1
    if not os.environ.get("APIFOX_ACCESS_TOKEN"):
        sys.stderr.write("missing APIFOX_ACCESS_TOKEN\n")
        return 1

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        method = message.get("method")
        message_id = message.get("id")
        if method == "initialize":
            respond(
                message_id,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "Fake Apifox MCP", "version": "0.0.0"},
                },
            )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            respond(message_id, {"tools": [] if MODE == "no_tools" else TOOLS})
        elif method == "tools/call":
            if MODE == "slow":
                continue
            params = message.get("params") or {}
            respond(message_id, handle_tool_call(str(params.get("name") or ""), params.get("arguments") or {}))
        elif message_id is not None:
            respond(message_id, error={"code": -32601, "message": "method not found: %s" % method})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
