"""测试公共设施：本地临时 HTTP 服务与临时配置。

所有测试都不访问外网；需要 HTTP 时使用 :class:`LocalHttpServer`。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Dict, List, Optional, Tuple

from hapitest.config import PlatformConfig

Handler = Callable[[str, str, Dict[str, str], bytes], Tuple[int, Dict[str, str], bytes]]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "HapiTestTestServer/1.0"

    def _dispatch(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        handler: Handler = self.server.app_handler  # type: ignore[attr-defined]
        record = {
            "method": self.command,
            "path": self.path,
            "headers": {k: v for k, v in self.headers.items()},
            "body": body.decode("utf-8", "replace"),
        }
        self.server.records.append(record)  # type: ignore[attr-defined]
        status, headers, payload = handler(self.command, self.path, dict(self.headers.items()), body)
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    do_GET = _dispatch
    do_POST = _dispatch
    do_PUT = _dispatch
    do_PATCH = _dispatch
    do_DELETE = _dispatch
    do_HEAD = _dispatch
    do_OPTIONS = _dispatch

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音
        return


class LocalHttpServer:
    """上下文管理器形式的本机测试服务。"""

    def __init__(self, handler: Optional[Handler] = None) -> None:
        self.handler = handler or default_handler
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None
        self.records: List[Dict[str, Any]] = []

    def __enter__(self) -> "LocalHttpServer":
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.app_handler = self.handler  # type: ignore[attr-defined]
        self.httpd.records = self.records  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        if self.thread is not None:
            self.thread.join(timeout=5)
            self.thread = None

    @property
    def port(self) -> int:
        assert self.httpd is not None
        return int(self.httpd.server_address[1])

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:%d" % self.port


def default_handler(method: str, path: str, headers: Dict[str, str], body: bytes) -> Tuple[int, Dict[str, str], bytes]:
    """默认路由：/ok、/echo、/json、/slow、/error、/redirect。"""
    path_only = path.split("?", 1)[0]
    if path_only == "/ok":
        return 200, {"Content-Type": "application/json; charset=utf-8"}, json.dumps({"code": 0, "msg": "ok"}).encode()
    if path_only == "/echo":
        payload = {
            "method": method,
            "path": path,
            "headers": headers,
            "body": body.decode("utf-8", "replace"),
        }
        return 200, {"Content-Type": "application/json; charset=utf-8"}, json.dumps(payload, ensure_ascii=False).encode()
    if path_only == "/json":
        payload = {"code": 0, "data": {"id": 42, "name": "hapitest", "items": [1, 2, 3], "nested": {"token": "abc"}}}
        return 200, {"Content-Type": "application/json"}, json.dumps(payload).encode()
    if path_only == "/text":
        return 200, {"Content-Type": "text/plain; charset=utf-8"}, "hello hapitest".encode()
    if path_only == "/error":
        return 500, {"Content-Type": "application/json"}, json.dumps({"code": 5000, "msg": "boom"}).encode()
    if path_only == "/redirect":
        return 302, {"Location": "/ok"}, b""
    if path_only == "/slow":
        import time

        time.sleep(0.4)
        return 200, {"Content-Type": "text/plain"}, b"slow"
    if path_only == "/set-cookie":
        return 200, {"Content-Type": "text/plain", "Set-Cookie": "sid=abc123; Path=/"}, b"cookie"
    if path_only == "/gzip":
        import gzip

        raw = json.dumps({"code": 0, "gzipped": True}).encode()
        return 200, {"Content-Type": "application/json", "Content-Encoding": "gzip"}, gzip.compress(raw)
    if path_only == "/big":
        return 200, {"Content-Type": "text/plain"}, b"x" * 4096
    return 404, {"Content-Type": "application/json"}, json.dumps({"code": 4040, "msg": "not found"}).encode()


class TempConfig:
    """临时数据目录 + 默认配置，避免污染真实 data/。"""

    def __init__(self) -> None:
        self._tmp: Optional[TemporaryDirectory] = None
        self.config: PlatformConfig = None  # type: ignore[assignment]

    def __enter__(self) -> PlatformConfig:
        self._tmp = TemporaryDirectory(prefix="hapitest-test-")
        self.config = PlatformConfig(root_dir=Path(__file__).resolve().parent.parent, data_dir=Path(self._tmp.name))
        self.config.ensure_dirs()
        return self.config

    def __exit__(self, *exc: Any) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None
