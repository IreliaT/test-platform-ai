"""``hapitest.http_client`` 的单元测试。

全部使用 ``tests.support`` 提供的本机临时 HTTP 服务与临时配置，不访问外网；
网络错误场景使用「已关闭端口」（连接被拒绝）与「只监听不 accept 的端口」（读取超时），
不使用固定 sleep，保证测试稳定且快速。
"""

from __future__ import annotations

import base64
import json
import socket
import ssl
import unittest
import zlib
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

from hapitest import http_client, redact
from hapitest.http_client import PreparedRequest, RequestBlocked, check_url_allowed, prepare, send
from hapitest.models import AuthSpec, KeyValue, RequestSpec
from hapitest.variables import VariableResolver
from tests.support import LocalHttpServer, TempConfig

Handler = Any
Response = Tuple[int, Dict[str, str], bytes]


class _FakeHttpsConnection:
    """假的 HTTPS 连接：只记录构造参数，用于断言超时与 TLS 上下文透传。"""

    instances: List["_FakeHttpsConnection"] = []

    def __init__(self, host: str, port: Any = None, timeout: Any = None, context: Any = None) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.context = context
        self.sock = None
        _FakeHttpsConnection.instances.append(self)

    def connect(self) -> None:
        raise ConnectionRefusedError("fake connect refused")

    def close(self) -> None:
        return None


def _free_port() -> int:
    """借一个已关闭的本机端口（后续连接会被拒绝）。"""
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _deflate_handler(method: str, path: str, headers: Dict[str, str], body: bytes) -> Response:
    """自定义路由：``/zlib`` 返回 zlib 包装的 deflate，``/raw`` 返回裸 deflate。"""
    raw = json.dumps({"code": 0, "deflated": True}).encode()
    if path.startswith("/raw"):
        compressor = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
        payload = compressor.compress(raw) + compressor.flush()
    else:
        payload = zlib.compress(raw)
    return 200, {"Content-Type": "application/json", "Content-Encoding": "deflate"}, payload


def _text_json_handler(method: str, path: str, headers: Dict[str, str], body: bytes) -> Response:
    """Content-Type 为 text/plain，但响应体是 JSON 文本。"""
    return 200, {"Content-Type": "text/plain; charset=utf-8"}, b'{"code": 0, "from": "text"}'


def _echo_any_handler(method: str, path: str, headers: Dict[str, str], body: bytes) -> Response:
    """任意路径都回显请求（用于验证路径参数替换后的真实请求行）。"""
    payload = {
        "method": method,
        "path": path,
        "headers": headers,
        "body": body.decode("utf-8", "replace"),
    }
    return 200, {"Content-Type": "application/json; charset=utf-8"}, json.dumps(payload, ensure_ascii=False).encode()


class HttpClientTestBase(unittest.TestCase):
    """公共夹具：本地 HTTP 服务 + 临时配置。

    服务按测试类启动一次（``LocalHttpServer.stop()`` 需要等 serve_forever 轮询，
    逐个用例启停会显著拖慢测试），每个用例前清空请求记录。
    """

    server: LocalHttpServer
    config: Any

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.server = LocalHttpServer()
        cls.server.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.__exit__(None, None, None)
        super().tearDownClass()

    def setUp(self) -> None:
        super().setUp()
        self.server.records.clear()
        temp = TempConfig()
        self.config = temp.__enter__()
        self.addCleanup(temp.__exit__, None, None, None)

    # ------------------------------------------------------------------ 辅助
    def run_request(self, spec: RequestSpec, **kwargs: Any) -> Any:
        return http_client.request(spec, self.config, base_url=self.server.base_url, **kwargs)

    def last_record(self) -> Dict[str, Any]:
        self.assertTrue(self.server.records, "本地服务没有收到任何请求")
        return self.server.records[-1]

    def sent_header(self, record: Dict[str, Any], name: str) -> Optional[str]:
        lowered = {str(key).lower(): value for key, value in record["headers"].items()}
        return lowered.get(name.lower())

    def received_header(self, response: Any, name: str) -> Optional[str]:
        lowered = {str(key).lower(): value for key, value in response.headers.items()}
        return lowered.get(name.lower())


# --------------------------------------------------------------------------- #
# 请求构建
# --------------------------------------------------------------------------- #
class PrepareTest(HttpClientTestBase):
    """prepare：URL 拼接、路径参数、默认头、认证、请求体与脱敏预览。"""

    def test_preview_contains_contract_keys(self) -> None:
        prepared = prepare(RequestSpec(path="/ok"), base_url=self.server.base_url)
        for key in ("method", "url", "headers", "body", "body_type"):
            self.assertIn(key, prepared.preview)
        self.assertEqual("GET", prepared.preview["method"])
        self.assertEqual(prepared.url, prepared.preview["url"])
        self.assertTrue(prepared.preview["headers"]["User-Agent"].startswith("HapiTest/"))
        self.assertEqual("*/*", prepared.preview["headers"]["Accept"])

    def test_url_join_rules(self) -> None:
        self.assertEqual("http://host/a", prepare(RequestSpec(path="/a"), base_url="http://host/").url)
        self.assertEqual("http://host/api/a", prepare(RequestSpec(path="a"), base_url="http://host/api").url)
        self.assertEqual("http://host/a", prepare(RequestSpec(path="/a"), base_url="http://host").url)
        self.assertEqual("http://host/", prepare(RequestSpec(path="/"), base_url="http://host/").url)
        # 重复斜杠折叠（scheme 的 // 保留）
        self.assertEqual("http://host/api/x", prepare(RequestSpec(path="/x"), base_url="http://host//api/").url)

    def test_absolute_path_ignores_base_url(self) -> None:
        prepared = prepare(RequestSpec(path="http://127.0.0.1:9/ok"), base_url="http://example.com/api")
        self.assertEqual("http://127.0.0.1:9/ok", prepared.url)

    def test_path_params_from_query_and_header(self) -> None:
        spec = RequestSpec(
            path="/echo/{id}/{kind}",
            query=[KeyValue("id", "42")],
            headers=[KeyValue("kind", "book")],
        )
        prepared = prepare(spec, base_url=self.server.base_url)
        self.assertEqual("/echo/42/book?id=42", prepared.url[len(self.server.base_url):])
        self.assertNotIn("unresolved_path_params", prepared.preview)

    def test_colon_style_path_param(self) -> None:
        prepared = prepare(
            RequestSpec(path="/echo/:uid", query=[KeyValue("uid", "7")]),
            base_url=self.server.base_url,
        )
        self.assertEqual("/echo/7?uid=7", prepared.url[len(self.server.base_url):])

    def test_unresolved_path_param_kept_and_recorded(self) -> None:
        prepared = prepare(RequestSpec(path="/x/{missing}"), base_url=self.server.base_url)
        self.assertEqual("/x/{missing}", prepared.url[len(self.server.base_url):])
        self.assertEqual(["missing"], prepared.preview["unresolved_path_params"])

    def test_path_param_resolved_from_variable(self) -> None:
        resolver = VariableResolver([{"id": 9}])
        prepared = prepare(RequestSpec(path="/users/{id}"), base_url=self.server.base_url, resolver=resolver)
        self.assertEqual("/users/9", prepared.url[len(self.server.base_url):])
        self.assertNotIn("unresolved_path_params", prepared.preview)

    def test_default_headers_do_not_override_explicit(self) -> None:
        prepared = prepare(
            RequestSpec(path="/ok", headers=[KeyValue("User-Agent", "custom-agent"), KeyValue("Accept", "text/plain")]),
            base_url=self.server.base_url,
        )
        self.assertEqual("custom-agent", prepared.headers["User-Agent"])
        self.assertEqual("text/plain", prepared.headers["Accept"])

    def test_query_encoding_keeps_slash_and_colon(self) -> None:
        prepared = prepare(
            RequestSpec(path="/echo", query=[KeyValue("u", "a/b:c"), KeyValue("empty", "")]),
            base_url=self.server.base_url,
        )
        self.assertTrue(prepared.url.endswith("/echo?u=a/b:c&empty="), prepared.url)

    def test_auth_variants(self) -> None:
        bearer = prepare(RequestSpec(path="/ok", auth=AuthSpec(type="bearer", token="t0k")), base_url=self.server.base_url)
        self.assertEqual("Bearer t0k", bearer.headers["Authorization"])

        custom_scheme = prepare(
            RequestSpec(path="/ok", auth=AuthSpec(type="bearer", token="t0k", scheme="Token")),
            base_url=self.server.base_url,
        )
        self.assertEqual("Token t0k", custom_scheme.headers["Authorization"])

        basic = prepare(
            RequestSpec(path="/ok", auth=AuthSpec(type="basic", username="u", password="p")),
            base_url=self.server.base_url,
        )
        expected = "Basic " + base64.b64encode(b"u:p").decode("ascii")
        self.assertEqual(expected, basic.headers["Authorization"])

        header_key = prepare(
            RequestSpec(path="/ok", auth=AuthSpec(type="api_key", key="X-Api-Key", value="k1")),
            base_url=self.server.base_url,
        )
        self.assertEqual("k1", header_key.headers["X-Api-Key"])

        query_key = prepare(
            RequestSpec(path="/ok", auth=AuthSpec(type="api_key", key="api_key", value="k2", location="query")),
            base_url=self.server.base_url,
        )
        self.assertIn("api_key=k2", query_key.url)
        self.assertNotIn("api_key", query_key.headers)
        # redact.redact_url 会重新编码 query，掩码在 URL 中可能呈现为 %2A...
        self.assertIn("api_key=******", unquote(query_key.preview["url"]))
        self.assertNotIn("k2", json.dumps(query_key.preview, ensure_ascii=False))

        for auth_type in ("inherit", "none"):
            prepared = prepare(RequestSpec(path="/ok", auth=AuthSpec(type=auth_type)), base_url=self.server.base_url)
            self.assertNotIn("Authorization", prepared.headers)

    def test_body_variants(self) -> None:
        json_body = prepare(
            RequestSpec(method="POST", path="/echo", body_type="json", body={"a": 1, "中文": "值"}),
            base_url=self.server.base_url,
        )
        self.assertEqual({"a": 1, "中文": "值"}, json.loads(json_body.body.decode("utf-8")))
        self.assertEqual("application/json; charset=utf-8", json_body.headers["Content-Type"])

        json_text = prepare(
            RequestSpec(method="POST", path="/echo", body_type="json", body='{"a": 1}'),
            base_url=self.server.base_url,
        )
        self.assertEqual(b'{"a": 1}', json_text.body)

        json_plain = prepare(
            RequestSpec(method="POST", path="/echo", body_type="json", body="hello"),
            base_url=self.server.base_url,
        )
        self.assertEqual("hello", json.loads(json_plain.body.decode("utf-8")))

        form = prepare(
            RequestSpec(method="POST", path="/echo", body_type="form", body={"a": "1", "b": "x y"}),
            base_url=self.server.base_url,
        )
        self.assertEqual("application/x-www-form-urlencoded", form.headers["Content-Type"])
        self.assertEqual({"a": ["1"], "b": ["x y"]}, parse_qs(form.body.decode("utf-8")))

        multipart = prepare(
            RequestSpec(method="POST", path="/echo", body_type="multipart", body={"field": "value"}),
            base_url=self.server.base_url,
        )
        self.assertEqual(
            "multipart/form-data; boundary=----HapiTestBoundary", multipart.headers["Content-Type"]
        )
        self.assertIn(b'name="field"', multipart.body)
        self.assertIn(b"------HapiTestBoundary", multipart.body)
        self.assertTrue(multipart.body.rstrip().endswith(b"------HapiTestBoundary--"))

        raw = prepare(
            RequestSpec(method="POST", path="/echo", body_type="raw", body="hello 中文"),
            base_url=self.server.base_url,
        )
        self.assertEqual("text/plain", raw.headers["Content-Type"])
        self.assertEqual("hello 中文".encode("utf-8"), raw.body)

        none = prepare(
            RequestSpec(method="GET", path="/echo", body_type="none", body={"a": 1}),
            base_url=self.server.base_url,
        )
        self.assertIsNone(none.body)
        self.assertNotIn("Content-Type", none.headers)
        self.assertEqual("", none.preview["body"])

    def test_request_spec_cookies_become_cookie_header(self) -> None:
        prepared = prepare(
            RequestSpec(path="/echo", cookies=[KeyValue("sid", "1"), KeyValue("theme", "dark")]),
            base_url=self.server.base_url,
        )
        self.assertEqual("sid=1; theme=dark", prepared.headers["Cookie"])

    def test_preview_redacts_headers_url_and_body(self) -> None:
        spec = RequestSpec(
            method="POST",
            path="/echo",
            body_type="json",
            body={"username": "u", "password": "p@ss", "nested": {"access_token": "t-secret"}},
            headers=[
                KeyValue("Authorization", "Bearer raw-token"),
                KeyValue("Cookie", "sid=raw-cookie"),
                KeyValue("X-Api-Key", "raw-key"),
            ],
            query=[KeyValue("token", "raw-query-token"), KeyValue("q", "1")],
        )
        prepared = prepare(spec, base_url=self.server.base_url)
        preview = prepared.preview

        # 真实请求保持原值，只有预览被脱敏
        self.assertEqual("Bearer raw-token", prepared.headers["Authorization"])
        self.assertIn("p@ss", prepared.body.decode("utf-8"))

        self.assertEqual(redact.MASK, preview["headers"]["Authorization"])
        self.assertEqual(redact.MASK, preview["headers"]["Cookie"])
        self.assertEqual(redact.MASK, preview["headers"]["X-Api-Key"])
        # redact.redact_url 会重新编码 query，掩码在 URL 中可能呈现为 %2A...
        self.assertIn("token=******", unquote(preview["url"]))
        self.assertIn("******", preview["body"])

        dumped = json.dumps(preview, ensure_ascii=False)
        for secret in ("raw-token", "raw-cookie", "raw-key", "raw-query-token", "p@ss", "t-secret"):
            self.assertNotIn(secret, dumped)

    def test_preview_masks_form_and_raw_secrets(self) -> None:
        form = prepare(
            RequestSpec(method="POST", path="/echo", body_type="form", body={"user": "u", "password": "p"}),
            base_url=self.server.base_url,
        )
        self.assertEqual("user=u&password=******", form.preview["body"])

        raw = prepare(
            RequestSpec(method="POST", path="/echo", body_type="raw", body='{"password": "p"}'),
            base_url=self.server.base_url,
        )
        self.assertNotIn('"password": "p"', raw.preview["body"])
        self.assertIn("******", raw.preview["body"])

    def test_timeout_and_flags_defaults(self) -> None:
        prepared = prepare(RequestSpec(path="/ok"), base_url=self.server.base_url)
        self.assertEqual(http_client.DEFAULT_TIMEOUT_MS, prepared.timeout_ms)
        self.assertTrue(prepared.follow_redirects)
        self.assertTrue(prepared.verify_tls)

        custom = prepare(
            RequestSpec(path="/ok", timeout_ms=1234, follow_redirects=False, verify_tls=False),
            base_url=self.server.base_url,
        )
        self.assertEqual(1234, custom.timeout_ms)
        self.assertFalse(custom.follow_redirects)
        self.assertFalse(custom.verify_tls)


# --------------------------------------------------------------------------- #
# 变量替换
# --------------------------------------------------------------------------- #
class VariableSubstitutionTest(HttpClientTestBase):
    """变量替换：路径、query、header、body。"""

    def test_resolver_substitutes_everywhere(self) -> None:
        resolver = VariableResolver(
            [{"base": self.server.base_url, "id": 7, "who": "hapitest"}]
        )
        spec = RequestSpec(
            method="POST",
            path="/echo/${id}",
            base_url="${base}",
            query=[KeyValue("q", "${who}")],
            headers=[KeyValue("X-Var", "${who}-${id}")],
            body_type="json",
            body={"name": "${who}", "id": "${id}"},
        )
        prepared = prepare(spec, resolver=resolver)
        self.assertEqual("%s/echo/7?q=hapitest" % self.server.base_url, prepared.url)
        self.assertEqual("hapitest-7", prepared.headers["X-Var"])

        # 默认服务只路由 /echo，这里用「任意路径回显」服务验证真实请求行
        with LocalHttpServer(_echo_any_handler) as server:
            live_resolver = VariableResolver([{"base": server.base_url, "id": 7, "who": "hapitest"}])
            prepared = prepare(
                RequestSpec(
                    method="POST",
                    path="/echo/${id}",
                    base_url="${base}",
                    query=[KeyValue("q", "${who}")],
                    headers=[KeyValue("X-Var", "${who}-${id}")],
                    body_type="json",
                    body={"name": "${who}", "id": "${id}"},
                ),
                resolver=live_resolver,
            )
            response = send(prepared, self.config)
            record = server.records[-1]

        self.assertTrue(response.ok, response.error)
        self.assertEqual(200, response.status)
        record = server.records[-1]
        self.assertEqual("/echo/7?q=hapitest", record["path"])
        self.assertEqual("hapitest-7", self.sent_header(record, "X-Var"))
        payload = json.loads(record["body"])
        self.assertEqual("hapitest", payload["name"])
        self.assertEqual("7", str(payload["id"]))

    def test_missing_variable_keeps_original_text(self) -> None:
        resolver = VariableResolver([{}])
        prepared = prepare(RequestSpec(path="/echo/${nope}"), base_url=self.server.base_url, resolver=resolver)
        self.assertIn("${nope}", prepared.url)
        missing = set(resolver.missing_names()) | set(resolver.unresolved_names())
        self.assertIn("nope", missing)


# --------------------------------------------------------------------------- #
# 发送请求
# --------------------------------------------------------------------------- #
class SendTest(HttpClientTestBase):
    """send / request：真实 HTTP 往返。"""

    def test_get_with_query_and_headers(self) -> None:
        spec = RequestSpec(
            path="/echo",
            query=[
                KeyValue("a", "1"),
                KeyValue("b", ""),
                KeyValue("u", "a/b:c"),
                KeyValue("skip", "x", enabled=False),
            ],
            headers=[KeyValue("X-Test", "hello")],
        )
        response = self.run_request(spec)
        self.assertTrue(response.ok, response.error)
        self.assertEqual(200, response.status)
        self.assertEqual("GET", response.method)
        self.assertEqual("%s/echo?a=1&b=&u=a/b:c" % self.server.base_url, response.url)
        self.assertEqual("GET", response.body_json["method"])
        self.assertEqual("/echo?a=1&b=&u=a/b:c", response.body_json["path"])

        record = self.last_record()
        self.assertEqual("/echo?a=1&b=&u=a/b:c", record["path"])
        self.assertEqual("hello", self.sent_header(record, "X-Test"))
        self.assertTrue(str(self.sent_header(record, "User-Agent")).startswith("HapiTest/"))
        self.assertEqual("*/*", self.sent_header(record, "Accept"))
        self.assertNotIn("skip", record["path"])

    def test_json_post_echo(self) -> None:
        body = {"name": "中文", "count": 3, "nested": {"ok": True}}
        response = self.run_request(RequestSpec(method="POST", path="/echo", body_type="json", body=body))
        self.assertTrue(response.ok, response.error)
        self.assertEqual(200, response.status)

        record = self.last_record()
        self.assertEqual("application/json; charset=utf-8", self.sent_header(record, "Content-Type"))
        self.assertEqual(body, json.loads(record["body"]))
        # 服务端回显的 body 与响应 JSON 一致
        self.assertEqual(body, json.loads(response.body_json["body"]))

    def test_form_post_echo(self) -> None:
        spec = RequestSpec(
            method="POST",
            path="/echo",
            body_type="form",
            body={"a": "1", "b": "x y", "c": "中文"},
        )
        response = self.run_request(spec)
        self.assertTrue(response.ok, response.error)
        record = self.last_record()
        self.assertEqual("application/x-www-form-urlencoded", self.sent_header(record, "Content-Type"))
        self.assertEqual({"a": ["1"], "b": ["x y"], "c": ["中文"]}, parse_qs(record["body"]))

    def test_multipart_post_echo(self) -> None:
        spec = RequestSpec(
            method="POST",
            path="/echo",
            body_type="multipart",
            body={"field": "value", "token": "secret-token"},
        )
        response = self.run_request(spec)
        self.assertTrue(response.ok, response.error)

        record = self.last_record()
        content_type = str(self.sent_header(record, "Content-Type"))
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="), content_type)
        boundary = content_type.split("boundary=", 1)[1]
        self.assertIn('Content-Disposition: form-data; name="field"', record["body"])
        self.assertIn("value", record["body"])
        self.assertIn("--%s" % boundary, record["body"])
        self.assertIn("--%s--" % boundary, record["body"])
        # 真实请求体不脱敏，预览脱敏
        self.assertIn("secret-token", record["body"])
        self.assertNotIn("secret-token", json.dumps(response.request_preview, ensure_ascii=False))

    def test_raw_post_echo(self) -> None:
        response = self.run_request(
            RequestSpec(method="POST", path="/echo", body_type="raw", body="hello 中文")
        )
        self.assertTrue(response.ok, response.error)
        record = self.last_record()
        self.assertEqual("hello 中文", record["body"])
        self.assertEqual("text/plain", self.sent_header(record, "Content-Type"))

    def test_http_error_status_is_not_a_failure(self) -> None:
        response = self.run_request(RequestSpec(path="/error"))
        self.assertTrue(response.ok, response.error)
        self.assertEqual(500, response.status)
        self.assertEqual("", response.error)
        self.assertEqual(5000, response.body_json["code"])

    def test_redirect_followed_and_recorded(self) -> None:
        response = self.run_request(RequestSpec(path="/redirect"))
        self.assertTrue(response.ok, response.error)
        self.assertEqual(200, response.status)
        self.assertTrue(response.url.endswith("/ok"), response.url)
        self.assertEqual(2, len(self.server.records))

    def test_redirect_not_followed(self) -> None:
        response = self.run_request(RequestSpec(path="/redirect", follow_redirects=False))
        self.assertTrue(response.ok, response.error)
        self.assertEqual(302, response.status)
        self.assertEqual("/ok", self.received_header(response, "Location"))
        self.assertEqual(1, len(self.server.records))

    def test_gzip_is_decompressed(self) -> None:
        response = self.run_request(RequestSpec(path="/gzip"))
        self.assertTrue(response.ok, response.error)
        self.assertEqual({"code": 0, "gzipped": True}, response.body_json)
        self.assertEqual("gzip", self.received_header(response, "Content-Encoding"))

    def test_deflate_is_decompressed(self) -> None:
        with LocalHttpServer(_deflate_handler) as server:
            wrapped = http_client.request(RequestSpec(path="/zlib"), self.config, base_url=server.base_url)
            raw = http_client.request(RequestSpec(path="/raw"), self.config, base_url=server.base_url)
        self.assertEqual({"code": 0, "deflated": True}, wrapped.body_json)
        self.assertEqual({"code": 0, "deflated": True}, raw.body_json)

    def test_body_json_parsed_even_for_text_content_type(self) -> None:
        with LocalHttpServer(_text_json_handler) as server:
            response = http_client.request(RequestSpec(path="/json-as-text"), self.config, base_url=server.base_url)
        self.assertEqual({"code": 0, "from": "text"}, response.body_json)

    def test_body_json_is_none_for_plain_text(self) -> None:
        response = self.run_request(RequestSpec(path="/text"))
        self.assertTrue(response.ok, response.error)
        self.assertEqual("hello hapitest", response.body_text)
        self.assertIsNone(response.body_json)

    def test_cookie_jar_round_trip(self) -> None:
        jar: Dict[str, str] = {}
        first = self.run_request(RequestSpec(path="/set-cookie"), cookie_jar=jar)
        self.assertTrue(first.ok, first.error)
        self.assertEqual({"sid": "abc123"}, jar)

        second = self.run_request(RequestSpec(path="/echo"), cookie_jar=jar)
        self.assertTrue(second.ok, second.error)
        self.assertIn("sid=abc123", str(self.sent_header(self.last_record(), "Cookie")))

    def test_cookie_from_request_spec_is_sent(self) -> None:
        response = self.run_request(RequestSpec(path="/echo", cookies=[KeyValue("theme", "dark")]))
        self.assertTrue(response.ok, response.error)
        self.assertEqual("theme=dark", self.sent_header(self.last_record(), "Cookie"))

    def test_auth_headers_reach_server(self) -> None:
        response = self.run_request(RequestSpec(path="/echo", auth=AuthSpec(type="bearer", token="t0k")))
        self.assertTrue(response.ok, response.error)
        self.assertEqual("Bearer t0k", self.sent_header(self.last_record(), "Authorization"))

    def test_slow_response_is_measured(self) -> None:
        response = self.run_request(RequestSpec(path="/slow", timeout_ms=5000))
        self.assertTrue(response.ok, response.error)
        self.assertEqual("slow", response.body_text)
        self.assertGreaterEqual(response.duration_ms, 350)

    def test_response_truncated_by_max_bytes(self) -> None:
        self.config.max_response_bytes = 1024
        response = self.run_request(RequestSpec(path="/big"))
        self.assertTrue(response.ok, response.error)
        self.assertTrue(response.truncated)
        self.assertEqual(1024, response.size_bytes)
        self.assertEqual(1024, len(response.body_text))

    def test_body_text_truncated_by_capture_bytes(self) -> None:
        self.config.capture_body_bytes = 100
        response = self.run_request(RequestSpec(path="/big"))
        self.assertTrue(response.ok, response.error)
        self.assertTrue(response.truncated)
        self.assertEqual(100, len(response.body_text))
        self.assertEqual(4096, response.size_bytes)

    def test_small_response_not_truncated(self) -> None:
        response = self.run_request(RequestSpec(path="/big"))
        self.assertTrue(response.ok, response.error)
        self.assertFalse(response.truncated)
        self.assertEqual(4096, response.size_bytes)

    def test_request_convenience_returns_response(self) -> None:
        response = http_client.request(RequestSpec(path="/ok"), self.config, base_url=self.server.base_url)
        self.assertTrue(response.ok, response.error)
        self.assertEqual({"code": 0, "msg": "ok"}, response.body_json)
        self.assertEqual(self.server.base_url + "/ok", response.url)
        self.assertEqual("GET", response.method)
        self.assertIn("headers", response.request_preview)


# --------------------------------------------------------------------------- #
# 网络异常与超时
# --------------------------------------------------------------------------- #
class ErrorHandlingTest(HttpClientTestBase):
    """网络异常都被归一化成 ok=False 的响应。"""

    def test_connection_refused(self) -> None:
        port = _free_port()
        self.config.connect_timeout_ms = 2000
        response = http_client.request(
            RequestSpec(path="/ok"), self.config, base_url="http://127.0.0.1:%d" % port
        )
        self.assertFalse(response.ok)
        self.assertIsNone(response.status)
        self.assertIn("连接被拒绝", response.error)
        self.assertGreaterEqual(response.duration_ms, 0)
        self.assertEqual("", response.body_text)
        self.assertEqual(0, response.size_bytes)

    def test_read_timeout_on_unresponsive_host(self) -> None:
        # 只监听、不 accept 的端口：TCP 握手成功但永远没有响应 → 读取超时
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        port = int(listener.getsockname()[1])

        prepared = prepare(RequestSpec(path="/ok", timeout_ms=300), base_url="http://127.0.0.1:%d" % port)
        self.config.connect_timeout_ms = 2000
        response = send(prepared, self.config)
        self.assertFalse(response.ok)
        self.assertIsNone(response.status)
        self.assertIn("超时", response.error)

    def test_connect_timeout_and_tls_context_propagation(self) -> None:
        prepared = PreparedRequest(
            method="GET",
            url="https://127.0.0.1:1/ok",
            headers={"Accept": "*/*"},
            body=None,
            timeout_ms=5000,
            follow_redirects=True,
            verify_tls=False,
            preview={"method": "GET", "url": "https://127.0.0.1:1/ok"},
        )
        self.config.connect_timeout_ms = 700
        original = http_client.http.client.HTTPSConnection
        http_client.http.client.HTTPSConnection = _FakeHttpsConnection  # type: ignore[assignment]
        try:
            response = send(prepared, self.config)
        finally:
            http_client.http.client.HTTPSConnection = original  # type: ignore[assignment]

        self.assertFalse(response.ok)
        self.assertIn("连接被拒绝", response.error)
        self.assertTrue(_FakeHttpsConnection.instances)
        connection = _FakeHttpsConnection.instances[-1]
        self.assertEqual(0.7, connection.timeout)
        self.assertIsNotNone(connection.context)
        self.assertEqual(ssl.CERT_NONE, connection.context.verify_mode)

    def test_ssl_context_modes(self) -> None:
        self.assertEqual(ssl.CERT_REQUIRED, http_client.build_ssl_context(True).verify_mode)
        self.assertEqual(ssl.CERT_NONE, http_client.build_ssl_context(False).verify_mode)

    def test_verify_tls_attribute_propagates(self) -> None:
        self.assertFalse(prepare(RequestSpec(path="/ok", verify_tls=False)).verify_tls)
        self.assertTrue(prepare(RequestSpec(path="/ok")).verify_tls)

    def test_describe_exception_messages(self) -> None:
        self.assertIn("超时", http_client.describe_exception(socket.timeout("timed out")))
        self.assertIn("连接被拒绝", http_client.describe_exception(ConnectionRefusedError(61, "Connection refused")))
        self.assertIn("TLS", http_client.describe_exception(ssl.SSLError("handshake failed")))
        self.assertIn("网络错误", http_client.describe_exception(OSError("broken")))

    def test_invalid_url_returns_error_without_raising(self) -> None:
        response = http_client.request(RequestSpec(path="ftp://example.com/x"), self.config)
        self.assertFalse(response.ok)
        self.assertIsNone(response.status)
        self.assertIn("协议", response.error)


# --------------------------------------------------------------------------- #
# SSRF 防护
# --------------------------------------------------------------------------- #
class UrlGuardTest(HttpClientTestBase):
    """check_url_allowed：协议、黑白名单、链路本地地址。"""

    def test_scheme_and_host_rules(self) -> None:
        with TempConfig() as config:
            self.assertIsNone(check_url_allowed("http://127.0.0.1:1/ok", config))
            self.assertIsNone(check_url_allowed("https://example.com/x", config))
            self.assertIn("协议", str(check_url_allowed("ftp://example.com/x", config)))
            self.assertIn("协议", str(check_url_allowed("file:///etc/passwd", config)))
            self.assertIn("协议", str(check_url_allowed("not-a-url", config)))
            self.assertIn("主机名", str(check_url_allowed("http:///x", config)))

    def test_link_local_and_metadata_blocked(self) -> None:
        with TempConfig() as config:
            # 169.254.169.254 同时在默认 deny_hosts 与链路本地规则内，任一原因都算拦截
            for url in (
                "http://169.254.169.254/latest/meta-data/",
                "http://metadata.google.internal/computeMetadata/v1/",
            ):
                reason = check_url_allowed(url, config)
                self.assertIsNotNone(reason, url)
                self.assertTrue(reason.startswith("禁止") or reason.endswith("拒绝列表内"), reason)

            for url in ("http://169.254.10.10/", "http://[fe80::1]/"):
                reason = check_url_allowed(url, config)
                self.assertIsNotNone(reason, url)
                self.assertIn("禁止", str(reason))

    def test_deny_and_allow_lists(self) -> None:
        with TempConfig() as config:
            config.deny_hosts = ("blocked.example.com",)
            self.assertIsNotNone(check_url_allowed("http://blocked.example.com/x", config))
            self.assertIsNotNone(check_url_allowed("http://sub.blocked.example.com/x", config))
            self.assertIsNone(check_url_allowed("http://example.com/x", config))

            config.allow_hosts = ("example.com",)
            self.assertIsNotNone(check_url_allowed("http://127.0.0.1:1/x", config))
            self.assertIsNone(check_url_allowed("http://example.com/x", config))
            self.assertIsNone(check_url_allowed("http://api.example.com/x", config))

    def test_blocked_request_never_reaches_server(self) -> None:
        self.config.deny_hosts = ("127.0.0.1",)
        response = self.run_request(RequestSpec(path="/ok"))
        self.assertFalse(response.ok)
        self.assertIsNone(response.status)
        self.assertIn("拒绝列表", response.error)
        self.assertEqual([], self.server.records)

    def test_ensure_url_allowed_raises_request_blocked(self) -> None:
        with self.assertRaises(RequestBlocked) as context:
            http_client.ensure_url_allowed("http://169.254.10.10/", self.config)
        self.assertIn("禁止", str(context.exception))
        self.assertIsInstance(context.exception, Exception)
        self.assertEqual("http://169.254.10.10/", context.exception.url)


# --------------------------------------------------------------------------- #
# 预览可直接落库
# --------------------------------------------------------------------------- #
class PreviewSerializationTest(HttpClientTestBase):
    """预览必须是可 JSON 序列化的纯数据。"""

    def test_preview_is_json_serializable(self) -> None:
        spec = RequestSpec(
            method="POST",
            path="/echo/{id}",
            query=[KeyValue("id", "1")],
            headers=[KeyValue("X-Test", "v")],
            body_type="form",
            body={"a": "b"},
            auth=AuthSpec(type="bearer", token="t"),
        )
        prepared = prepare(spec, base_url=self.server.base_url)
        dumped = json.dumps(prepared.preview, ensure_ascii=False)
        restored = json.loads(dumped)
        self.assertEqual(prepared.preview["url"], restored["url"])
        self.assertEqual(urlsplit(prepared.url).path, "/echo/1")

    def test_response_preview_matches_prepared_preview(self) -> None:
        response = self.run_request(RequestSpec(path="/ok", headers=[KeyValue("X-Test", "v")]))
        self.assertEqual("v", response.request_preview["headers"]["X-Test"])
        self.assertEqual("GET", response.request_preview["method"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
