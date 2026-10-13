"""HTTP 服务：路由、响应信封、静态资源。"""

from __future__ import annotations

import json
import logging
import mimetypes
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Pattern, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

from .. import PLATFORM_NAME, PROTOCOL_VERSION, __version__
from ..config import PlatformConfig
from ..errors import HapiTestError, MethodNotAllowedError, NotFoundError, ValidationError
from ..repository import ProjectRegistry
from ..runner import RunManager

LOGGER = logging.getLogger("hapitest.server")

MAX_BODY_BYTES = 32 * 1024 * 1024


@dataclass
class Request:
    """一次请求的解析结果。"""

    method: str
    path: str
    raw_query: str
    headers: Dict[str, str]
    body: bytes
    query: Dict[str, List[str]] = field(default_factory=dict)

    def param(self, name: str, default: str = "") -> str:
        values = self.query.get(name)
        return values[0] if values else default

    def int_param(self, name: str, default: int, minimum: Optional[int] = None, maximum: Optional[int] = None) -> int:
        raw = self.param(name, "")
        try:
            value = int(raw) if raw != "" else int(default)
        except (TypeError, ValueError):
            raise ValidationError("参数 %s 必须是整数" % name)
        if minimum is not None and value < minimum:
            value = minimum
        if maximum is not None and value > maximum:
            value = maximum
        return value

    def json(self) -> Dict[str, Any]:
        if not self.body:
            return {}
        try:
            data = json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValidationError("请求体不是合法 JSON: %s" % exc)
        if not isinstance(data, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return data

    def header(self, name: str, default: str = "") -> str:
        lowered = {key.lower(): value for key, value in self.headers.items()}
        return lowered.get(name.lower(), default)


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "application/json; charset=utf-8"
    headers: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def json(cls, payload: Any, status: int = 200) -> "Response":
        return cls(
            status=status,
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            content_type="application/json; charset=utf-8",
        )

    @classmethod
    def ok(cls, data: Any = None) -> "Response":
        return cls.json({"ok": True, "data": data})

    @classmethod
    def text(cls, text: str, content_type: str = "text/plain; charset=utf-8", status: int = 200) -> "Response":
        return cls(status=status, body=text.encode("utf-8"), content_type=content_type)

    @classmethod
    def error(cls, exc: HapiTestError) -> "Response":
        return cls.json({"ok": False, "error": exc.to_dict()}, status=exc.status)


HandlerFunc = Callable[..., Any]


class Router:
    """极简正则路由表。"""

    def __init__(self) -> None:
        self._routes: List[Tuple[str, Pattern[str], HandlerFunc]] = []

    def add(self, method: str, pattern: str, handler: HandlerFunc) -> None:
        regex = re.compile("^%s$" % pattern)
        self._routes.append((method.upper(), regex, handler))

    def match(self, method: str, path: str) -> Tuple[HandlerFunc, Dict[str, str]]:
        allowed: List[str] = []
        for route_method, regex, handler in self._routes:
            found = regex.match(path)
            if not found:
                continue
            if route_method != method.upper():
                allowed.append(route_method)
                continue
            return handler, {key: unquote(value) for key, value in found.groupdict().items()}
        if allowed:
            raise MethodNotAllowedError("方法不支持: %s %s（允许 %s）" % (method, path, "/".join(sorted(set(allowed)))))
        raise NotFoundError("接口不存在: %s %s" % (method, path))


class App:
    """把配置、注册表、执行器与路由组装在一起。"""

    def __init__(
        self,
        config: PlatformConfig,
        registry: Optional[ProjectRegistry] = None,
        run_manager: Optional[RunManager] = None,
    ) -> None:
        self.config = config
        self.registry = registry or ProjectRegistry(config)
        self.runs = run_manager or RunManager(config, self.registry)
        self.started_at = time.time()
        self.router = Router()
        from . import api

        api.register(self.router)

    # --- 调度 ---
    def dispatch(self, request: Request) -> Response:
        try:
            handler, params = self.router.match(request.method, request.path)
        except HapiTestError as exc:
            return Response.error(exc)
        try:
            result = handler(self, request, **params)
        except HapiTestError as exc:
            return Response.error(exc)
        except BrokenPipeError:
            raise
        except Exception as exc:  # 兜底：任何未预期异常都变成 500，不中断服务
            LOGGER.exception("处理 %s %s 失败", request.method, request.path)
            return Response.json(
                {"ok": False, "error": {"code": "internal_error", "message": "%s: %s" % (type(exc).__name__, exc)}},
                status=500,
            )
        if isinstance(result, Response):
            return result
        return Response.ok(result)

    # --- 静态资源 ---
    def static_response(self, path: str) -> Response:
        web_dir = self.config.web_dir
        if not web_dir.exists():
            return Response.text("前端资源缺失：%s" % web_dir, status=500)
        relative = path[len("/assets/") :] if path.startswith("/assets/") else ""
        if relative:
            candidate = (web_dir / relative).resolve()
            if web_dir.resolve() not in candidate.parents and candidate != web_dir.resolve():
                return Response.error(NotFoundError("非法资源路径"))
            if candidate.is_file():
                content_type = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
                if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
                    content_type += "; charset=utf-8"
                return Response(status=200, body=candidate.read_bytes(), content_type=content_type)
            return Response.error(NotFoundError("静态资源不存在: %s" % relative))
        index = web_dir / "index.html"
        if not index.exists():
            return Response.text("前端入口缺失：%s" % index, status=500)
        return Response(status=200, body=index.read_bytes(), content_type="text/html; charset=utf-8")


class _RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "%s/%s" % (PLATFORM_NAME, __version__)

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def _handle(self) -> None:
        parsed = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            response = Response.json(
                {"ok": False, "error": {"code": "bad_request", "message": "请求体过大"}}, status=413
            )
        else:
            body = self.rfile.read(length) if length else b""
            request = Request(
                method=self.command,
                path=parsed.path or "/",
                raw_query=parsed.query,
                headers={key: value for key, value in self.headers.items()},
                body=body,
                query=parse_qs(parsed.query, keep_blank_values=True),
            )
            if request.path.startswith("/api/"):
                response = self.app.dispatch(request)
            else:
                response = self.app.static_response(request.path)

        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in response.headers.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD" and response.body:
            try:
                self.wfile.write(response.body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle
    do_PATCH = _handle
    do_HEAD = _handle
    do_OPTIONS = _handle

    def log_message(self, fmt: str, *args: Any) -> None:
        LOGGER.debug("%s - %s", self.address_string(), fmt % args)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    app: App


def create_server(config: PlatformConfig, host: Optional[str] = None, port: Optional[int] = None) -> Tuple[_Server, App]:
    """创建（但不启动）HTTP 服务，便于测试与嵌入式使用。"""
    app = App(config)
    address = (host or config.host, int(port if port is not None else config.port))
    httpd = _Server(address, _RequestHandler)
    httpd.app = app
    return httpd, app


def serve(config: PlatformConfig, port: Optional[int] = None, host: Optional[str] = None) -> None:
    """启动前台服务（阻塞）。"""
    httpd, app = create_server(config, host=host, port=port)
    address = httpd.server_address
    httpd.app.runs.start()
    LOGGER.info(
        "%s %s 已启动: http://%s:%d （数据目录 %s）",
        PLATFORM_NAME,
        __version__,
        address[0],
        address[1],
        config.data_dir,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("收到中断信号，正在停止…")
    finally:
        app.runs.stop()
        httpd.shutdown()
        httpd.server_close()
        app.registry.close()


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def health_payload(config: PlatformConfig, app: Optional[App] = None) -> Dict[str, Any]:
    return {
        "name": PLATFORM_NAME,
        "version": __version__,
        "protocol": PROTOCOL_VERSION,
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "uptime_s": int(time.time() - app.started_at) if app else 0,
        "data_dir": str(config.data_dir),
    }
