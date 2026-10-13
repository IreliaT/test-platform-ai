#!/usr/bin/env python3
"""本地演示/联调用的 mock 服务（仅标准库）。

用途：

- 首次体验平台：``python3 examples/mock_service.py`` 后运行内置的 demo 用例；
- 端到端自检：``python3 -m hapitest verify`` 会自动拉起本服务；
- 编写新用例时作为稳定的"被测系统"，不依赖任何真实业务环境。

返回体统一为 ``{"code": 0, "msg": "ok", "data": ...}``，与平台的默认断言
（``$.code == 0``、``$.data`` 存在）配合即可直接跑通。
"""

from __future__ import annotations

import argparse
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

USERS: Dict[int, Dict[str, Any]] = {
    1: {"id": 1, "name": "张三", "email": "zhangsan@example.com", "age": 28, "tags": ["vip", "beta"]},
    2: {"id": 2, "name": "李四", "email": "lisi@example.com", "age": 34, "tags": ["beta"]},
    3: {"id": 3, "name": "王五", "email": "wangwu@example.com", "age": 41, "tags": []},
}


def envelope(data: Any, code: int = 0, msg: str = "ok") -> Dict[str, Any]:
    return {"code": code, "msg": msg, "data": data}


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "HapiTestMock/1.0"

    def _read_body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return raw.decode("utf-8", "replace")

    def _send(self, status: int, payload: Any, headers: Optional[Dict[str, str]] = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Mock-Server", "hapitest")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # ---------------------------------------------------------------- 路由
    def _route(self, method: str) -> Tuple[int, Any, Optional[Dict[str, str]]]:
        from urllib.parse import parse_qs, urlsplit

        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query, keep_blank_values=True)
        body = self._read_body()

        if path in ("/v2/api-docs", "/v3/api-docs") and method in ("GET", "HEAD"):
            # 提供一份真实的 Swagger 2.0 契约，用于演练「从文档地址同步接口」这条路径。
            # host 按当前监听地址改写，这样平台探测到的 base_url 就是本服务的真实地址。
            spec_path = Path(__file__).resolve().parent / "demo_specs" / "demo_swagger2.json"
            if spec_path.exists():
                document = json.loads(spec_path.read_text(encoding="utf-8"))
                host, port = self.server.server_address[0], self.server.server_address[1]
                document["host"] = "%s:%d" % (host, port)
                return 200, document, None
            return 404, envelope(None, code=40402, msg="示例契约缺失: %s" % spec_path), None

        if path == "/api/health" and method in ("GET", "HEAD"):
            return 200, envelope({"status": "UP", "ts": int(time.time()), "version": "mock-1.0"}), None

        if path == "/api/users" and method == "GET":
            page = int((query.get("page") or ["1"])[0] or 1)
            size = int((query.get("size") or ["10"])[0] or 10)
            keyword = (query.get("keyword") or [""])[0]
            items: List[Dict[str, Any]] = list(USERS.values())
            if keyword:
                items = [item for item in items if keyword in item["name"] or keyword in item["email"]]
            start = max(0, (page - 1) * size)
            return 200, envelope({"list": items[start : start + size], "total": len(items), "page": page, "size": size}), None

        if path == "/api/users" and method == "POST":
            new_id = max(USERS) + 1 if USERS else 1
            record = {"id": new_id, "name": "新用户", "email": "new@example.com", "age": 20, "tags": []}
            if isinstance(body, dict):
                record.update({key: value for key, value in body.items() if key in ("name", "email", "age", "tags")})
            USERS[new_id] = record
            return 201, envelope(record), None

        matched = re.match(r"^/api/users/(?P<uid>[^/]+)$", path)
        if matched:
            raw_id = matched.group("uid")
            try:
                user_id = int(raw_id)
            except ValueError:
                return 400, envelope(None, code=40001, msg="用户 ID 必须是整数"), None
            if method in ("GET", "HEAD"):
                record = USERS.get(user_id)
                if record is None:
                    return 404, envelope(None, code=40401, msg="用户不存在"), None
                return 200, envelope(record), None
            if method == "PUT":
                record = USERS.get(user_id)
                if record is None:
                    return 404, envelope(None, code=40401, msg="用户不存在"), None
                if isinstance(body, dict):
                    record.update({key: value for key, value in body.items() if key in ("name", "email", "age", "tags")})
                return 200, envelope(record), None
            if method == "DELETE":
                existed = USERS.pop(user_id, None) is not None
                return (200, envelope({"deleted": existed}), None) if existed else (
                    404,
                    envelope(None, code=40401, msg="用户不存在"),
                    None,
                )

        if path == "/api/auth/login" and method == "POST":
            if not isinstance(body, dict) or not body.get("username"):
                return 400, envelope(None, code=40002, msg="缺少 username"), None
            return 200, envelope({"token": "mock-token-%d" % int(time.time()), "expires_in": 7200}), {
                "Set-Cookie": "mock_sid=abc123; Path=/"
            }

        if path == "/api/orders" and method == "GET":
            return 200, envelope({"list": [{"orderId": "MOCK-1", "amount": 12.5}], "total": 1}), None

        if path == "/api/slow" and method == "GET":
            time.sleep(2.0)
            return 200, envelope({"slow": True}), None

        if path == "/api/error" and method == "GET":
            return 500, envelope(None, code=50000, msg="模拟服务端错误"), None

        return 404, envelope(None, code=40400, msg="接口不存在: %s %s" % (method, path)), None

    # ---------------------------------------------------------------- 入口
    def _handle(self) -> None:
        try:
            status, payload, headers = self._route(self.command)
        except Exception as exc:  # mock 自身异常也要返回结构化错误
            status, payload, headers = 500, envelope(None, code=50099, msg="mock 内部错误: %s" % exc), None
        self._send(status, payload, headers)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle
    do_PATCH = _handle
    do_HEAD = _handle

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.server.verbose:  # type: ignore[attr-defined]
            print("[mock] %s - %s" % (self.address_string(), fmt % args), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="HapiTest 演示 mock 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), MockHandler)
    httpd.verbose = args.verbose  # type: ignore[attr-defined]
    print("mock 服务已启动: http://%s:%d/  （Ctrl+C 停止）" % (args.host, args.port), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nmock 服务已停止", flush=True)
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
