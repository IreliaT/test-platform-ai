"""MCP（Model Context Protocol）stdio 客户端。

用途：把支持 MCP 的接口文档服务（当前用于 **Apifox MCP Server**）作为平台的接口来源，
让"同步接口"这一步不依赖手工导出文件。

协议要点（stdio 传输）：

- 一行一个 JSON-RPC 2.0 消息（换行分隔，不是 LSP 的 Content-Length 分帧）；
- 握手顺序：``initialize`` → ``notifications/initialized`` → ``tools/list`` → ``tools/call``；
- 工具名可能带随机后缀（例如 ``read_project_oas_sppywa``），**必须按前缀匹配**，不能写死。

安全约定：访问令牌只通过子进程环境变量传递，不写入命令行、不落盘、不打印；
所有对外输出（含子进程 stderr）都经过掩码处理。
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import PLATFORM_NAME, __version__
from .errors import ValidationError

DEFAULT_COMMAND = ("npx", "-y", "apifox-mcp-server@latest")
DEFAULT_TOKEN_ENV = "APIFOX_ACCESS_TOKEN"
PROTOCOL_VERSION = "2024-11-05"

# 工具名前缀（Apifox MCP Server 会加随机后缀）
TOOL_READ_OAS = "read_project_oas"
TOOL_REFRESH_OAS = "refresh_project_oas"
TOOL_READ_REF = "read_project_oas_ref_resources"

MASK = "******"


def mask_secrets(text: str, secrets: Sequence[str]) -> str:
    """把可能出现在子进程输出里的令牌替换掉。"""
    if not text:
        return text
    masked = text
    for secret in secrets:
        if secret and len(str(secret)) >= 4:
            masked = masked.replace(str(secret), MASK)
    return masked


@dataclass
class ToolCallResult:
    ok: bool
    text: str = ""
    error: str = ""
    tool: str = ""
    is_error: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)


class McpStdioClient:
    """极简 MCP stdio 客户端：只实现握手、列工具、调用工具。"""

    def __init__(
        self,
        command: Sequence[str],
        env: Optional[Dict[str, str]] = None,
        timeout_s: float = 120.0,
        secrets: Sequence[str] = (),
        cwd: Optional[str] = None,
    ) -> None:
        self.command = [str(item) for item in command]
        self.extra_env = dict(env or {})
        self.timeout_s = float(timeout_s)
        self.secrets = tuple(secrets)
        self.cwd = cwd
        self.process: Optional[subprocess.Popen] = None
        self.stderr_lines: List[str] = []
        self._lock = threading.Lock()
        self._next_id = 0
        self._messages: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
        self.server_info: Dict[str, Any] = {}
        self.tools: List[Dict[str, Any]] = []

    # ---------------------------------------------------------------- 生命周期
    def __enter__(self) -> "McpStdioClient":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def start(self) -> None:
        executable = self.command[0]
        if shutil.which(executable) is None:
            raise ValidationError(
                "找不到命令 %s：MCP 方式需要 Node.js 18+（npx 随 Node 一起安装）。"
                "也可以改用文件导入：--file <导出的 OpenAPI/Postman/Apifox JSON>" % executable
            )
        env = dict(os.environ)
        env.update(self.extra_env)
        env.setdefault("npm_config_yes", "true")
        try:
            self.process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
                cwd=self.cwd,
            )
        except OSError as exc:
            raise ValidationError("启动 MCP 服务失败：%s" % exc)
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        threading.Thread(target=self._drain_stdout, daemon=True).start()

    def _drain_stdout(self) -> None:
        """把 stdout 的行读进队列。

        必须用独立线程：在管道上直接 readline() 会一直阻塞，超时判断永远不会生效
        （这正是"服务不响应时挂死"的根因）。
        """
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                text = line.strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except ValueError:
                    continue  # 非 JSON 输出（例如 npx 的提示行）
                self._messages.put(message)
        except (ValueError, OSError):
            pass
        finally:
            self._messages.put(None)  # EOF 哨兵：告诉等待方服务已结束

    def _drain_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            self.stderr_lines.append(mask_secrets(line.rstrip(), self.secrets))
            if len(self.stderr_lines) > 200:
                del self.stderr_lines[:100]

    def close(self) -> None:
        """结束子进程并关闭管道。

        必须显式关闭三条流：每次同步都会拉起一个 MCP 进程，
        只 terminate 不关流会泄漏文件描述符（长跑的服务端会累积）。
        """
        process = self.process
        self.process = None
        if process is None:
            return
        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                if stream is not None and not stream.closed:
                    stream.close()
            except (OSError, ValueError):
                pass

    # ---------------------------------------------------------------- 通信
    def stderr_tail(self, lines: int = 8) -> str:
        return "\n".join(self.stderr_lines[-lines:])

    def _send(self, payload: Dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None:
            raise ValidationError("MCP 服务未启动")
        try:
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise ValidationError("MCP 服务已退出：%s%s" % (exc, self._stderr_hint()))

    def _stderr_hint(self) -> str:
        tail = self.stderr_tail()
        return ("；服务输出：%s" % tail) if tail else ""

    def _read_result(self, message_id: int, timeout_s: Optional[float] = None) -> Dict[str, Any]:
        process = self.process
        if process is None:
            raise ValidationError("MCP 服务未启动")
        deadline = time.time() + float(timeout_s if timeout_s is not None else self.timeout_s)
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise ValidationError("MCP 调用超时（%.0fs）%s" % (self.timeout_s, self._stderr_hint()))
            try:
                message = self._messages.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                if process.poll() is not None and self._messages.empty():
                    raise ValidationError(
                        "MCP 服务提前退出（返回码 %s）%s" % (process.returncode, self._stderr_hint())
                    )
                continue
            if message is None:
                raise ValidationError(
                    "MCP 服务已关闭连接（返回码 %s）%s" % (process.poll(), self._stderr_hint())
                )
            if message.get("id") != message_id:
                continue  # 服务端通知或其它响应
            if "error" in message:
                error = message["error"] or {}
                raise ValidationError(
                    "MCP 调用失败：%s" % mask_secrets(str(error.get("message") or error), self.secrets)
                )
            return message.get("result") or {}

    def request(self, method: str, params: Optional[Dict[str, Any]] = None, timeout_s: Optional[float] = None) -> Dict[str, Any]:
        with self._lock:
            self._next_id += 1
            message_id = self._next_id
            self._send({"jsonrpc": "2.0", "id": message_id, "method": method, "params": params or {}})
            return self._read_result(message_id, timeout_s)

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    # ---------------------------------------------------------------- 高层动作
    def initialize(self) -> Dict[str, Any]:
        result = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": PLATFORM_NAME.lower(), "version": __version__},
            },
        )
        self.server_info = dict(result.get("serverInfo") or {})
        self.notify("notifications/initialized")
        return result

    def list_tools(self) -> List[Dict[str, Any]]:
        result = self.request("tools/list")
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise ValidationError("MCP 服务未返回工具列表%s" % self._stderr_hint())
        self.tools = [tool for tool in tools if isinstance(tool, dict)]
        return self.tools

    def find_tool(self, *prefixes: str) -> Optional[str]:
        """按前缀匹配工具名（服务会附加随机后缀，不能写死全名）。"""
        for prefix in prefixes:
            for tool in self.tools:
                name = str(tool.get("name") or "")
                if name == prefix or name.startswith(prefix + "_") or name.startswith(prefix):
                    return name
        return None

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None, timeout_s: Optional[float] = None) -> ToolCallResult:
        result = self.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout_s=timeout_s)
        chunks: List[str] = []
        for item in result.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                chunks.append(str(item.get("text") or ""))
        text = "\n".join(chunk for chunk in chunks if chunk)
        is_error = bool(result.get("isError"))
        if is_error and not text:
            text = "MCP 工具返回错误"
        return ToolCallResult(ok=not is_error, text=text, tool=name, is_error=is_error, raw=result)


def build_apifox_command(
    project_id: Optional[str] = None,
    doc_id: Optional[str] = None,
    spec_file: Optional[str] = None,
    api_base_url: Optional[str] = None,
    branch_id: Optional[str] = None,
    command: Optional[Sequence[str]] = None,
) -> List[str]:
    """组装 Apifox MCP Server 启动命令。

    ``--branch-id`` 决定读哪个分支：不传时服务读**主分支**，
    团队把接口放在迭代分支上时必须显式指定，否则会得到"只有几条接口"的假象。
    """
    parts = list(command) if command else list(DEFAULT_COMMAND)
    if project_id:
        parts.append("--project=%s" % project_id)
    elif doc_id:
        parts.append("--doc=%s" % doc_id)
    elif spec_file:
        parts.append("--oas=%s" % spec_file)
    else:
        raise ValidationError("MCP 方式需要提供 Apifox 项目 ID（--mcp-project）或文档 ID / OAS 文件")
    if branch_id:
        parts.append("--branch-id=%s" % branch_id)
    if api_base_url:
        parts.append("--apifox-api-base-url=%s" % api_base_url)
    return parts


def resolve_token(token_env: str = DEFAULT_TOKEN_ENV, env_file_values: Optional[Dict[str, str]] = None) -> Tuple[str, str]:
    """解析访问令牌：进程环境优先，其次 .env.local。

    令牌只留在内存里，不落盘、不打印。
    """
    token = os.environ.get(token_env)
    if not token and env_file_values:
        token = env_file_values.get(token_env)
    if not token:
        raise ValidationError(
            "缺少 Apifox 访问令牌：请设置环境变量 %s（Apifox → 账号设置 → API 访问令牌）。"
            "推荐写进项目根目录的 .env.local（该文件已被 .gitignore 排除），不要写在命令行里。" % token_env
        )
    return token, token_env


def fetch_apifox_openapi(
    project_id: Optional[str] = None,
    token: Optional[str] = None,
    command: Optional[Sequence[str]] = None,
    doc_id: Optional[str] = None,
    spec_file: Optional[str] = None,
    api_base_url: Optional[str] = None,
    branch_id: Optional[str] = None,
    token_env: str = DEFAULT_TOKEN_ENV,
    refresh: bool = True,
    timeout_s: float = 180.0,
    fetch_refs: bool = True,
    warnings: Optional[List[str]] = None,
) -> Tuple[str, Dict[str, Any]]:
    """通过 MCP 取回 Apifox 项目的 OpenAPI 文档。

    返回 ``(文档文本, 元信息)``。元信息包含服务端名称、使用的工具名与刷新结果，
    便于排查"拿到的到底是不是最新数据"。
    """
    warn = warnings if warnings is not None else []
    if not token:
        raise ValidationError("MCP 方式需要 Apifox 访问令牌")
    cmd = build_apifox_command(
        project_id=project_id,
        doc_id=doc_id,
        spec_file=spec_file,
        api_base_url=api_base_url,
        branch_id=branch_id,
        command=command,
    )
    env = {token_env: token}
    meta: Dict[str, Any] = {
        "command": " ".join(cmd[:3]),
        "project_id": project_id or "",
        "branch_id": branch_id or "(主分支)",
        "refreshed": False,
    }

    client = McpStdioClient(cmd, env=env, timeout_s=timeout_s, secrets=[token])
    with client:
        client.initialize()
        meta["server"] = (
            "%s %s" % (client.server_info.get("name", "unknown"), client.server_info.get("version", ""))
        ).strip()
        client.list_tools()
        meta["tools"] = [str(tool.get("name")) for tool in client.tools]

        if refresh:
            refresh_tool = client.find_tool(TOOL_REFRESH_OAS)
            if refresh_tool:
                try:
                    result = client.call_tool(refresh_tool, {})
                    meta["refreshed"] = result.ok
                    if not result.ok:
                        warn.append(
                            "刷新 Apifox 文档失败，将使用服务端已有缓存：%s"
                            % mask_secrets(result.text[:200], [token])
                        )
                except ValidationError as exc:
                    warn.append("刷新 Apifox 文档失败，将使用服务端已有缓存：%s" % exc.message)
            else:
                warn.append("MCP 服务未提供刷新工具（%s），可能读到的是本地缓存" % TOOL_REFRESH_OAS)

        read_tool = client.find_tool(TOOL_READ_OAS)
        if not read_tool:
            raise ValidationError(
                "MCP 服务未提供接口文档读取工具（期望前缀 %s），实际工具：%s"
                % (TOOL_READ_OAS, ", ".join(meta["tools"]) or "无")
            )
        result = client.call_tool(read_tool, {}, timeout_s=timeout_s)
        meta["tool"] = read_tool
        if not result.ok:
            # 服务端的报错里可能回显令牌，必须先掩码再往外抛
            message = mask_secrets(result.text or "工具返回错误", [token])
            hint = ""
            if "token" in message.lower() or "401" in message or "403" in message:
                hint = "（请检查 Apifox 访问令牌是否有效、是否属于该项目）"
            raise ValidationError("读取 Apifox 接口文档失败：%s%s" % (message[:300], hint))
        text = result.text.strip()
        if not text:
            raise ValidationError("Apifox MCP 返回了空文档，请确认项目 ID 与令牌权限")

        if fetch_refs and _has_external_refs(text):
            text = _inline_external_refs(client, text, warn, timeout_s)
    meta["bytes"] = len(text.encode("utf-8"))
    return text, meta


def _has_external_refs(text: str) -> bool:
    return '"$ref"' in text or "$ref:" in text


def _inline_external_refs(client: McpStdioClient, text: str, warn: List[str], timeout_s: float) -> str:
    """把拆分文件里的外部 $ref 拉回来内联。

    Apifox 的 OpenAPI 导出可能是多文件结构（``$ref: /paths/_get_pet.json``），
    主文件本身不含路径。这里通过 ``read_project_oas_ref_resources`` 一次性取回并内联；
    失败时只记 warning，不阻断导入（调用方会看到接口数为 0 并据此判断）。
    """
    try:
        document = json.loads(text)
    except ValueError:
        return text  # YAML 主文件：暂不处理外部引用
    refs: List[str] = []

    def collect(node: Any) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and not ref.startswith("#") and ref not in refs:
                refs.append(ref)
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for item in node:
                collect(item)

    collect(document)
    if not refs:
        return text
    tool = client.find_tool(TOOL_READ_REF)
    if not tool:
        warn.append("文档包含 %d 个外部 $ref，但 MCP 服务未提供 %s 工具，可能导入不完整" % (len(refs), TOOL_READ_REF))
        return text
    try:
        result = client.call_tool(tool, {"path": refs[:200]}, timeout_s=timeout_s)
    except ValidationError as exc:
        warn.append("读取外部 $ref 失败：%s" % exc.message)
        return text
    if not result.ok:
        warn.append("读取外部 $ref 失败：%s" % result.text[:200])
        return text
    resources = _parse_ref_resources(result.text)
    if not resources:
        warn.append("外部 $ref 返回内容无法解析，可能导入不完整")
        return text
    merged = _replace_refs(document, resources)
    warn.append("已内联 %d 个外部 $ref 文件" % len(resources))
    return json.dumps(merged, ensure_ascii=False)


def _parse_ref_resources(text: str) -> Dict[str, Any]:
    """解析 ref 资源工具返回的内容，兼容 JSON 整体或逐行 JSON。"""
    text = text.strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        # 形如 {"<path>": {...}} 或 {"resources": [...]}
        if "resources" in data and isinstance(data["resources"], list):
            merged: Dict[str, Any] = {}
            for item in data["resources"]:
                if isinstance(item, dict) and item.get("path"):
                    merged[str(item["path"])] = item.get("content")
            return merged
        return {str(k): v for k, v in data.items()}
    merged = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("path"):
            merged[str(item["path"])] = item.get("content")
    return merged


def _replace_refs(node: Any, resources: Dict[str, Any], depth: int = 0) -> Any:
    if depth > 8:
        return node
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and not ref.startswith("#") and ref in resources:
            content = resources[ref]
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except ValueError:
                    return node
            return _replace_refs(content, resources, depth + 1)
        return {key: _replace_refs(value, resources, depth + 1) for key, value in node.items()}
    if isinstance(node, list):
        return [_replace_refs(item, resources, depth + 1) for item in node]
    return node
