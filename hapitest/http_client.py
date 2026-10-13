"""HTTP 请求构建与发送（INTERFACES.md 1.5）。

职责划分：

- :func:`prepare`：把 :class:`hapitest.models.RequestSpec` 编译成可发送的
  :class:`PreparedRequest`——变量替换、路径参数替换、查询参数拼接、默认头、认证注入、
  请求体编码；同时产出**已脱敏**的 ``preview``，可直接落库、展示到界面或写入报告。
- :func:`send`：用 ``http.client`` 真正发起请求，负责连接/读取超时、TLS 校验开关、
  重定向、gzip/deflate 解压、Cookie 读写、响应体截断与异常归一化。
  **任何失败都以 ``HttpResponse(ok=False, error=...)`` 返回，绝不向调用方抛异常。**
- :func:`check_url_allowed`：SSRF 防护；返回中文拦截原因，``None`` 表示放行。
- :func:`request`：``prepare`` + ``send`` 的一次性便捷入口（执行引擎使用）。

实现约定（与契约的关系）：

1. ``preview`` 始终包含契约要求的 5 个键 ``method/url/headers/body/body_type``，且全部脱敏；
   当路径参数既没有同名的 query/header 条目、也没有同名变量时，额外给出
   ``unresolved_path_params``（仅在非空时出现），供界面提示未替换的占位符。
2. ``preview["body"]`` 统一为字符串（JSON 体为缩进后的脱敏 JSON 文本），便于界面与报告直接展示。
3. ``HttpResponse.size_bytes`` 是**实际读取到的字节数**：响应被 ``max_response_bytes`` 截断时，
   它是真实响应体长度的下界；``truncated=True`` 同时表示字节截断或 ``capture_body_bytes`` 字符截断。
4. ``multipart`` 只支持「普通文本字段」（不支持文件上传），边界固定为
   ``----HapiTestBoundary``，文件字段请改用 ``raw`` 或以 ``{"file": 路径}`` 之外的方式扩展。
5. 重定向最多跟随 5 次，且**每一跳都重新做 SSRF 校验**；301/302 的 POST 与 303 会按浏览器语义
   降级为 GET 并丢弃请求体（自定义请求头保留），307/308 保持原方法与请求体。
6. Cookie jar 是简单的 ``name -> value`` 映射：只解析 ``Set-Cookie`` 的首个 ``name=value``，
   ``Path``/``Domain`` 仅做宽松处理（不区分路径与子域），足够覆盖常见的会话串联场景。
"""

from __future__ import annotations

import base64
import gzip
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
import zlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote, urlencode, urljoin, urlsplit, urlunsplit

from . import redact
from .config import PlatformConfig
from .models import BODY_TYPES, HttpResponse, RequestSpec

try:  # hapitest/variables.py 与本模块并行开发：缺失时保持可导入，解析退化为“无变量”
    from .variables import VariableResolver
except Exception:  # pragma: no cover - 仅在变量模块尚未就绪时命中
    VariableResolver = Any  # type: ignore[assignment,misc]

__all__ = [
    "DEFAULT_TIMEOUT_MS",
    "MAX_REDIRECTS",
    "MULTIPART_BOUNDARY",
    "PreparedRequest",
    "RequestBlocked",
    "build_ssl_context",
    "check_url_allowed",
    "describe_exception",
    "ensure_url_allowed",
    "prepare",
    "request",
    "send",
]

#: ``RequestSpec.timeout_ms`` 缺省时的兜底超时（与 ``PlatformConfig.default_timeout_ms`` 保持一致）
DEFAULT_TIMEOUT_MS = 15000
#: 最多跟随的重定向次数
MAX_REDIRECTS = 5
#: multipart 固定边界，便于稳定断言与排查
MULTIPART_BOUNDARY = "----HapiTestBoundary"
#: 单次 socket 读取的块大小
READ_CHUNK_SIZE = 8192
#: 需要跟随 ``Location`` 的状态码
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
#: 预览体最大长度（字符），避免超大请求体撑爆数据库/报告
PREVIEW_BODY_LIMIT = 20000

PROTOCOL_VALUES = ("http", "https")
LOCAL_METADATA_HOST = "169.254.169.254"
LOCAL_METADATA_PREFIX = "169.254."

_MISSING = object()

_BRACE_PARAM_RE = re.compile(r"\{([^{}/]+)\}")
_COLON_PARAM_RE = re.compile(r":([A-Za-z_][A-Za-z0-9_]*)")
_MULTI_SLASH_RE = re.compile(r"/{2,}")
_CHARSET_RE = re.compile(r"charset\s*=\s*[\"']?([A-Za-z0-9_\-]+)", re.IGNORECASE)
_JSON_PAIR_RE = re.compile(r'("(?P<jkey>[^"\\]{1,128})"\s*:\s*)(?P<jvalue>"(?:[^"\\]|\\.)*"|[^,}\]\s]+)')
_FORM_PAIR_RE = re.compile(r"(?P<fkey>[A-Za-z0-9_.\[\]%\-]{1,128})=(?P<fvalue>[^&;\s]*)")
_JSON_CONTENT_HINTS = ("application/json", "text/json", "+json")


class RequestBlocked(Exception):
    """请求被安全策略拦截（SSRF / 协议 / 主机白名单）。

    :func:`send` 内部不会抛出该异常（改为返回 ``ok=False`` 的响应）；
    需要「快速失败」的调用方（界面试运行、CLI 预检）可用 :func:`ensure_url_allowed`。
    """

    def __init__(self, reason: str, url: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.url = url


@dataclass
class PreparedRequest:
    """可直接发送的请求：真实内容 + 已脱敏预览。"""

    method: str = "GET"
    url: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    body: Optional[bytes] = None
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    follow_redirects: bool = True
    verify_tls: bool = True
    preview: Dict[str, Any] = field(default_factory=dict)


@dataclass
class _Exchange:
    """单次（不跟随重定向的）请求结果，供 :func:`send` 内部使用。"""

    url: str = ""
    status: Optional[int] = None
    headers: Dict[str, str] = field(default_factory=dict)
    cookie_headers: List[str] = field(default_factory=list)
    body: bytes = b""
    truncated: bool = False
    error: str = ""


# --------------------------------------------------------------------------- #
# 公共辅助
# --------------------------------------------------------------------------- #
def build_ssl_context(verify_tls: bool = True) -> ssl.SSLContext:
    """按 ``verify_tls`` 生成 SSL context（``False`` 时使用未校验上下文）。"""
    if verify_tls:
        return ssl.create_default_context()
    return ssl._create_unverified_context()  # noqa: SLF001 - 契约要求使用该私有工厂


def describe_exception(exc: BaseException) -> str:
    """把底层异常翻译成中文说明，格式固定为 ``<中文说明>: <异常>``。"""
    if isinstance(exc, (socket.timeout, TimeoutError)):
        label = "请求超时"
    elif isinstance(exc, ssl.SSLError):
        label = "TLS 校验或握手失败"
    elif isinstance(exc, ConnectionRefusedError):
        label = "连接被拒绝"
    elif isinstance(exc, socket.gaierror):
        label = "域名解析失败"
    elif isinstance(exc, ConnectionResetError):
        label = "连接被重置"
    elif isinstance(exc, http.client.HTTPException):
        label = "HTTP 协议错误"
    elif isinstance(exc, OSError):
        label = "网络错误"
    else:
        label = "请求异常"
    detail = str(exc) or exc.__class__.__name__
    return "%s: %s" % (label, detail)


def ensure_url_allowed(url: str, config: PlatformConfig) -> None:
    """URL 被拦截时抛 :class:`RequestBlocked`，否则静默返回。"""
    reason = check_url_allowed(url, config)
    if reason:
        raise RequestBlocked(reason, url)


def check_url_allowed(url: str, config: PlatformConfig) -> Optional[str]:
    """SSRF 防护：返回中文拦截原因；``None`` 表示放行。

    规则（按顺序判定）：

    1. 只允许 ``http``/``https``；
    2. 必须有主机名；
    3. ``config.deny_hosts_set()`` 命中（精确或子域后缀）即拒绝；
    4. 云元数据地址 ``169.254.169.254`` 与整段链路本地地址（``169.254.*`` / ``fe80::/10``）拒绝；
    5. ``config.allow_hosts_set()`` 非空时，主机名必须等于其中一项或以 ``.<项>`` 结尾。
    """
    text = str(url or "").strip()
    if not text:
        return "请求地址为空"
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        return "请求地址无法解析: %s" % exc

    scheme = (parts.scheme or "").lower()
    if scheme not in PROTOCOL_VALUES:
        return "仅允许 http/https 协议，当前为: %s" % (scheme or "空")
    host = (parts.hostname or "").strip().lower()
    if not host:
        return "请求地址缺少主机名"
    try:
        host.encode("ascii")
    except UnicodeEncodeError:
        return "主机名包含非 ASCII 字符，可能是伪造地址: %s" % host

    for entry in config.deny_hosts_set():
        if _host_matches(host, entry):
            return "主机 %s 在拒绝列表内" % host

    if host == LOCAL_METADATA_HOST or host.startswith(LOCAL_METADATA_PREFIX):
        return "禁止访问链路本地/云元数据地址: %s" % host
    try:
        if ipaddress.ip_address(host).is_link_local:
            return "禁止访问链路本地/云元数据地址: %s" % host
    except ValueError:
        pass

    allow = config.allow_hosts_set()
    if allow and not any(_host_matches(host, entry) for entry in allow):
        return "主机 %s 不在允许列表内" % host
    return None


def _host_matches(host: str, entry: str) -> bool:
    """主机名是否命中白/黑名单项（支持 ``*.example.com``、``.example.com``、子域后缀与 ``host:port``）。"""
    candidate = str(entry or "").strip().lower()
    if not candidate:
        return False
    if candidate.startswith("*"):
        candidate = candidate[1:]
    candidate = candidate.lstrip(".")
    if ":" in candidate and not candidate.startswith("["):
        # 允许配置里带端口，如 ``127.0.0.1:8801``（IPv6 需写成 ``[::1]``）
        candidate = candidate.split(":", 1)[0]
    candidate = candidate.strip("[]")
    if not candidate:
        return False
    return host == candidate or host.endswith("." + candidate)


# --------------------------------------------------------------------------- #
# 请求构建
# --------------------------------------------------------------------------- #
def prepare(
    request: RequestSpec,
    base_url: str = "",
    resolver: Optional[VariableResolver] = None,
) -> PreparedRequest:
    """把请求描述编译成 :class:`PreparedRequest`（含脱敏预览）。

    :param request: 用例/接口模板中的请求描述
    :param base_url: 环境级 base_url，``request.base_url`` 优先
    :param resolver: 变量解析器；为 ``None`` 时按「无变量」处理
    """
    method = (str(request.method or "GET").strip() or "GET").upper()
    body_type = str(request.body_type or "none").strip().lower()
    if body_type not in BODY_TYPES:
        body_type = "none" if request.body is None else "raw"

    params = _collect_path_param_values(request, resolver)
    raw_base = _resolve_text(request.base_url if request.base_url else base_url, resolver)
    raw_path = _resolve_text(request.path or "/", resolver)
    url = _build_url(raw_base, raw_path)
    url, unresolved = _apply_path_params(url, params, resolver)
    url = _collapse_path_slashes(url)

    headers: Dict[str, str] = {}
    for item in request.headers or []:
        if not item.enabled or not str(item.name or "").strip():
            continue
        headers[str(item.name).strip()] = _resolve_text(item.value, resolver)

    cookie_header = _build_cookie_header(request, resolver)
    if cookie_header and not _has_header(headers, "Cookie"):
        headers["Cookie"] = cookie_header

    if not _has_header(headers, "User-Agent"):
        headers["User-Agent"] = "HapiTest/%s" % _platform_version()
    if not _has_header(headers, "Accept"):
        headers["Accept"] = "*/*"

    auth_query = _apply_auth(request.auth, headers, resolver)

    query_pairs: List[Tuple[str, Any]] = []
    for item in request.query or []:
        if not item.enabled or not str(item.name or "").strip():
            continue
        query_pairs.append((str(item.name).strip(), _resolve_any(item.value, resolver)))
    query_pairs.extend(auth_query)
    if query_pairs:
        url = _append_query(url, _encode_query(query_pairs))

    body, preview_body = _build_body(body_type, request.body, resolver, headers)
    if request.auth.type == "aliyun_sls":
        from .sls_auth import sign
        sign(method, url, headers, body, request.auth, resolver)

    preview: Dict[str, Any] = {
        "method": method,
        "url": redact.redact_url(url, sensitive_params=[name for name, _ in auth_query]),
        "headers": redact.redact_headers(headers),
        "body": preview_body,
        "body_type": body_type,
    }
    if unresolved:
        # 契约要求 5 个固定键；未替换的路径参数是附加提示，仅在存在时出现
        preview["unresolved_path_params"] = sorted(set(unresolved))

    return PreparedRequest(
        method=method,
        url=url,
        headers=headers,
        body=body,
        timeout_ms=int(request.timeout_ms or 0) or DEFAULT_TIMEOUT_MS,
        follow_redirects=bool(request.follow_redirects) and request.auth.type != "aliyun_sls",
        verify_tls=bool(request.verify_tls),
        preview=preview,
    )


def request(
    request: RequestSpec,
    config: PlatformConfig,
    base_url: str = "",
    resolver: Optional[VariableResolver] = None,
    cookie_jar: Optional[Dict[str, str]] = None,
) -> HttpResponse:
    """``prepare`` + ``send``：执行引擎使用的一次性入口，永不抛异常。"""
    try:
        prepared = prepare(request, base_url=base_url, resolver=resolver)
    except Exception as exc:  # 构建阶段同样不能把异常抛给调用方
        return HttpResponse(
            ok=False,
            status=None,
            method=(str(request.method or "GET").strip() or "GET").upper(),
            error="请求构建失败: %s" % (exc or exc.__class__.__name__),
        )
    return send(prepared, config, cookie_jar=cookie_jar)


# --------------------------------------------------------------------------- #
# 发送
# --------------------------------------------------------------------------- #
def send(
    prepared: PreparedRequest,
    config: PlatformConfig,
    cookie_jar: Optional[Dict[str, str]] = None,
) -> HttpResponse:
    """发送请求并返回 :class:`HttpResponse`；失败时 ``ok=False``、``status=None``。

    :param cookie_jar: 可选的 ``name -> value`` 映射；发送时带上其中 Cookie，
        收到 ``Set-Cookie`` 时写回同一个字典（因此可跨请求串联会话）。
    """
    started = time.perf_counter()
    jar: Dict[str, str] = cookie_jar if cookie_jar is not None else {}
    method = (str(prepared.method or "GET").strip() or "GET").upper()

    reason = check_url_allowed(prepared.url, config)
    if reason:
        return _failure(prepared, prepared.url, reason, started)

    timeout_ms = int(prepared.timeout_ms or 0) or int(getattr(config, "default_timeout_ms", 0) or 0) or DEFAULT_TIMEOUT_MS
    read_timeout = max(timeout_ms, 1) / 1000.0
    connect_timeout_ms = int(getattr(config, "connect_timeout_ms", 0) or 0) or timeout_ms
    connect_timeout = max(connect_timeout_ms, 1) / 1000.0
    max_bytes = int(getattr(config, "max_response_bytes", 0) or 0)
    capture_chars = int(getattr(config, "capture_body_bytes", 0) or 0)

    current_url = prepared.url
    current_method = method
    current_body = prepared.body
    redirects = 0

    while True:
        exchange = _send_once(
            current_method,
            current_url,
            current_body,
            prepared,
            jar,
            connect_timeout,
            read_timeout,
            max_bytes,
        )
        if exchange.error:
            return _failure(prepared, exchange.url or current_url, exchange.error, started)

        _store_cookies(exchange.cookie_headers, jar)

        location = _header_value(exchange.headers, "Location")
        if (
            prepared.follow_redirects
            and exchange.status in REDIRECT_STATUSES
            and location
            and redirects < MAX_REDIRECTS
        ):
            next_url = urljoin(exchange.url, location.strip())
            blocked = check_url_allowed(next_url, config)
            if blocked:
                # 重定向目标同样要做 SSRF 校验，避免被跳转绕过
                return _failure(prepared, next_url, blocked, started)
            redirects += 1
            if exchange.status == 303 or (exchange.status in (301, 302) and current_method == "POST"):
                current_method = "GET"
                current_body = None
            current_url = next_url
            continue

        body_text, truncated = _decode_body(exchange.body, exchange.headers, capture_chars, exchange.truncated)
        return HttpResponse(
            ok=True,
            status=exchange.status,
            headers=dict(exchange.headers),
            body_text=body_text,
            body_json=_parse_json(body_text, exchange.headers),
            duration_ms=_elapsed_ms(started),
            url=exchange.url,
            method=current_method,
            request_preview=dict(prepared.preview or {}),
            error="",
            truncated=truncated,
            size_bytes=len(exchange.body),
        )


def _send_once(
    method: str,
    url: str,
    body: Optional[bytes],
    prepared: PreparedRequest,
    jar: Mapping[str, str],
    connect_timeout: float,
    read_timeout: float,
    max_bytes: int,
) -> _Exchange:
    """执行一次请求（不跟随重定向）；任何异常都转成 ``_Exchange.error``。"""
    exchange = _Exchange(url=url)
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").strip()
    if not host:
        exchange.error = "请求地址缺少主机名: %s" % url
        return exchange
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        exchange.error = "请求地址端口非法: %s" % exc
        return exchange

    target = urlunsplit(("", "", parts.path or "/", parts.query, ""))
    headers = dict(prepared.headers or {})
    _apply_jar_cookies(headers, jar)

    conn: Any = None
    try:
        if scheme == "https":
            context = build_ssl_context(prepared.verify_tls)
            conn = http.client.HTTPSConnection(host, port, timeout=connect_timeout, context=context)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=connect_timeout)
        # 分开控制「连接超时」与「读取超时」：先按连接超时建连，再切到读取超时
        conn.connect()
        if conn.sock is not None:
            conn.sock.settimeout(read_timeout)
        conn.request(method, target, body=body, headers=headers)
        response = conn.getresponse()
        exchange.status = int(response.status)
        pairs = [(str(name), str(value)) for name, value in response.getheaders()]
        exchange.headers = _merge_headers(pairs)
        exchange.cookie_headers = [value for name, value in pairs if name.lower() == "set-cookie"]
        exchange.body, exchange.truncated = _read_body(response, max_bytes)
        try:
            response.close()
        except Exception:  # pragma: no cover - 关闭失败不影响结果
            pass
    except Exception as exc:  # 包括 socket.timeout / SSLError / HTTPException / OSError
        exchange.error = describe_exception(exc)
        exchange.status = None
        exchange.body = b""
        exchange.truncated = False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # pragma: no cover
                pass
    return exchange


def _read_body(response: Any, limit: int) -> Tuple[bytes, bool]:
    """分块读取响应体；超过 ``limit`` 字节即停止，返回 ``(bytes, truncated)``。

    ``limit <= 0`` 表示不限制。超限时内容被裁剪到 ``limit``，因此调用方拿到的
    ``len(bytes)`` 是真实长度的下界。
    """
    chunks: List[bytes] = []
    total = 0
    while True:
        chunk = response.read(READ_CHUNK_SIZE)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if limit > 0 and total >= limit:
            break
    data = b"".join(chunks)
    if limit > 0 and len(data) > limit:
        return data[:limit], True
    if limit > 0 and len(data) == limit:
        # 刚好等于上限时再探一个字节，判断是否还有剩余
        if response.read(1):
            return data, True
    return data, False


def _decode_body(
    raw: bytes,
    headers: Mapping[str, str],
    capture_chars: int,
    truncated: bool,
) -> Tuple[str, bool]:
    """解压 + 按 charset 解码 + 按 ``capture_body_bytes`` 截断字符。"""
    data = _decompress(raw, _header_value(headers, "Content-Encoding") or "")
    charset = _charset_of(_header_value(headers, "Content-Type") or "")
    text = data.decode(charset, "replace")
    if capture_chars > 0 and len(text) > capture_chars:
        text = text[:capture_chars]
        truncated = True
    return text, truncated


def _decompress(data: bytes, content_encoding: str) -> bytes:
    """解压 gzip/x-gzip/deflate；失败时原样返回（绝不抛异常）。"""
    if not data:
        return data
    name = content_encoding.split(",")[0].strip().lower()
    try:
        if name in ("gzip", "x-gzip"):
            return gzip.decompress(data)
        if name == "deflate":
            try:
                return zlib.decompress(data)
            except zlib.error:
                # 部分服务端发的是 raw deflate（无 zlib 头）
                return zlib.decompress(data, -zlib.MAX_WBITS)
    except Exception:
        return data
    return data


def _charset_of(content_type: str) -> str:
    match = _CHARSET_RE.search(content_type or "")
    return match.group(1) if match else "utf-8"


def _parse_json(text: str, headers: Mapping[str, str]) -> Any:
    """尽力解析 JSON：Content-Type 像 JSON 或文本本身能解析时填充；失败返回 ``None``。"""
    if not text:
        return None
    content_type = (_header_value(headers, "Content-Type") or "").lower()
    looks_json = any(token in content_type for token in _JSON_CONTENT_HINTS)
    stripped = text.strip()
    if not looks_json and not stripped.startswith(("{", "[")) and len(stripped) > 64:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _merge_headers(pairs: Sequence[Tuple[str, str]]) -> Dict[str, str]:
    """响应头合并为字典：同名头用 ``, `` 连接；``Set-Cookie`` 只保留第一条（Cookie jar 记录全部）。"""
    merged: Dict[str, str] = {}
    for name, value in pairs:
        existing_key = None
        for key in merged:
            if key.lower() == name.lower():
                existing_key = key
                break
        if existing_key is None:
            merged[name] = value
        elif existing_key.lower() == "set-cookie":
            continue
        else:
            merged[existing_key] = "%s, %s" % (merged[existing_key], value)
    return merged


def _store_cookies(cookie_headers: Sequence[str], jar: Dict[str, str]) -> None:
    """把 ``Set-Cookie`` 写入 jar（宽松模式：忽略 Path/Domain，``Max-Age=0`` 视为删除）。"""
    for raw in cookie_headers:
        segments = [item.strip() for item in str(raw).split(";")]
        if not segments:
            continue
        first = segments[0]
        if "=" not in first:
            continue
        name, _, value = first.partition("=")
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        attributes: Dict[str, str] = {}
        for segment in segments[1:]:
            key, _, attr_value = segment.partition("=")
            attributes[key.strip().lower()] = attr_value.strip()
        if attributes.get("max-age") == "0":
            jar.pop(name, None)
            continue
        jar[name] = value


def _apply_jar_cookies(headers: Dict[str, str], jar: Mapping[str, str]) -> None:
    """把 jar 中的 Cookie 合并进 ``Cookie`` 头（同名以显式 header 为准）。"""
    if not jar:
        return
    existing = _header_value(headers, "Cookie") or ""
    parts = [item.strip() for item in existing.split(";") if item.strip()]
    names = {item.split("=", 1)[0].strip().lower() for item in parts}
    for name, value in jar.items():
        if str(name).strip().lower() in names:
            continue
        parts.append("%s=%s" % (name, value))
    if parts:
        headers["Cookie"] = "; ".join(parts)


def _failure(prepared: PreparedRequest, url: str, message: str, started: float) -> HttpResponse:
    """构造失败响应（``ok=False``、``status=None``）。"""
    return HttpResponse(
        ok=False,
        status=None,
        headers={},
        body_text="",
        body_json=None,
        duration_ms=_elapsed_ms(started),
        url=url,
        method=(str(prepared.method or "GET").strip() or "GET").upper(),
        request_preview=dict(prepared.preview or {}),
        error=message,
        truncated=False,
        size_bytes=0,
    )


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


# --------------------------------------------------------------------------- #
# 构建细节
# --------------------------------------------------------------------------- #
def _platform_version() -> str:
    try:
        from . import __version__

        return str(__version__)
    except Exception:  # pragma: no cover - 防御式兜底
        return "0.1.0"


def _resolve_text(value: Any, resolver: Optional[VariableResolver]) -> str:
    text = "" if value is None else str(value)
    if resolver is None:
        return text
    return resolver.resolve_text(text)


def _resolve_any(value: Any, resolver: Optional[VariableResolver]) -> Any:
    if resolver is None:
        return value
    return resolver.resolve_value(value)


def _is_absolute_url(text: str) -> bool:
    lowered = (text or "").strip().lower()
    return lowered.startswith("http://") or lowered.startswith("https://")


def _build_url(base_url: str, path: str) -> str:
    """拼接 base_url 与 path：避免重复斜杠，保留 path 上已有的 query。"""
    raw_path = (path or "").strip()
    if _is_absolute_url(raw_path):
        return raw_path
    base = (base_url or "").strip()
    if not base:
        return raw_path or "/"
    if not raw_path:
        return base
    if base.endswith("/") and raw_path.startswith("/"):
        return base + raw_path[1:]
    if not base.endswith("/") and not raw_path.startswith(("/", "?", "#")):
        return base + "/" + raw_path
    return base + raw_path


def _collapse_path_slashes(url: str) -> str:
    """折叠路径中的重复斜杠（保留 scheme 的 ``//`` 与 query 原样）。"""
    if "//" not in url:
        return url
    parts = urlsplit(url)
    if parts.scheme.lower() not in PROTOCOL_VALUES:
        return url
    collapsed = _MULTI_SLASH_RE.sub("/", parts.path)
    return urlunsplit((parts.scheme, parts.netloc, collapsed, parts.query, parts.fragment))


def _collect_path_param_values(request: RequestSpec, resolver: Optional[VariableResolver]) -> Dict[str, str]:
    """收集可用于路径参数替换的取值：同名的 query 条目优先，其次同名的 header 条目。"""
    values: Dict[str, str] = {}
    for item in request.query or []:
        if item.enabled and str(item.name or "").strip():
            values.setdefault(str(item.name).strip(), _resolve_text(item.value, resolver))
    for item in request.headers or []:
        if item.enabled and str(item.name or "").strip():
            values.setdefault(str(item.name).strip(), _resolve_text(item.value, resolver))
    return values


def _apply_path_params(
    url: str,
    params: Mapping[str, str],
    resolver: Optional[VariableResolver],
) -> Tuple[str, List[str]]:
    """替换路径部分（query 之前）的 ``{name}`` / ``:name``。

    取值优先级：同名 query/header 条目 → 同名变量 → 保留原文并记录到未解析列表。
    """
    unresolved: List[str] = []

    def lookup(name: str) -> Optional[str]:
        if name in params:
            return params[name]
        if resolver is not None:
            value = resolver.get(name, _MISSING)
            if value is not _MISSING and value is not None:
                return str(value)
        return None

    def replace(match: "re.Match[str]") -> str:
        name = match.group(1).strip()
        if not name:
            return match.group(0)
        value = lookup(name)
        if value is None:
            unresolved.append(name)
            return match.group(0)
        return value

    parts = urlsplit(url)
    path = _BRACE_PARAM_RE.sub(replace, parts.path)
    path = _COLON_PARAM_RE.sub(replace, path)
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment)), unresolved


def _build_cookie_header(request: RequestSpec, resolver: Optional[VariableResolver]) -> str:
    pairs: List[str] = []
    for item in request.cookies or []:
        if not item.enabled or not str(item.name or "").strip():
            continue
        pairs.append("%s=%s" % (str(item.name).strip(), _resolve_text(item.value, resolver)))
    return "; ".join(pairs)


def _apply_auth(
    auth: Any,
    headers: Dict[str, str],
    resolver: Optional[VariableResolver],
) -> List[Tuple[str, str]]:
    """注入认证信息；返回需要追加到 query 的 ``(name, value)`` 列表。

    ``inherit``/``none`` 不注入任何内容（继承关系由执行引擎在调用前解析完毕）。
    """
    if auth is None:
        return []
    auth_type = str(getattr(auth, "type", "") or "none").strip().lower()
    if auth_type in ("", "none", "inherit"):
        return []
    if auth_type == "bearer":
        token = _resolve_text(getattr(auth, "token", ""), resolver)
        if token:
            scheme = _resolve_text(getattr(auth, "scheme", "") or "Bearer", resolver).strip() or "Bearer"
            headers["Authorization"] = "%s %s" % (scheme, token)
        return []
    if auth_type == "basic":
        username = _resolve_text(getattr(auth, "username", ""), resolver)
        password = _resolve_text(getattr(auth, "password", ""), resolver)
        raw = ("%s:%s" % (username, password)).encode("utf-8")
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        return []
    if auth_type in ("api_key", "header"):
        key = _resolve_text(getattr(auth, "key", ""), resolver).strip()
        value = _resolve_text(getattr(auth, "value", ""), resolver)
        if not key:
            return []
        location = str(getattr(auth, "location", "header") or "header").strip().lower()
        if auth_type == "api_key" and location == "query":
            return [(key, value)]
        headers[key] = value
        return []
    return []


def _encode_query(pairs: Sequence[Tuple[str, Any]]) -> str:
    """查询参数编码：doseq 友好；``quote_via=quote`` 且保留 ``/`` ``:``（值里常带路径或时间）。"""
    return urlencode(list(pairs), doseq=True, quote_via=quote, safe="/:")


def _append_query(url: str, query: str) -> str:
    if not query:
        return url
    parts = urlsplit(url)
    merged = parts.query + ("&" if parts.query else "") + query
    return urlunsplit((parts.scheme, parts.netloc, parts.path, merged, parts.fragment))


def _has_header(headers: Mapping[str, str], name: str) -> bool:
    return _header_value(headers, name) is not None


def _header_value(headers: Mapping[str, str], name: str) -> Optional[str]:
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return value
    return None


def _build_body(
    body_type: str,
    raw_body: Any,
    resolver: Optional[VariableResolver],
    headers: Dict[str, str],
) -> Tuple[Optional[bytes], str]:
    """按 body_type 生成请求体，并在 ``headers`` 上补默认 Content-Type。

    返回 ``(body_bytes, 脱敏后的预览文本)``。
    """
    resolved = _resolve_any(raw_body, resolver)

    if body_type == "none":
        return None, ""

    if body_type == "json":
        if resolved is None:
            return None, ""
        if isinstance(resolved, str):
            text = resolved.strip()
            body_text = text if text and _parses_as_json(text) else json.dumps(resolved, ensure_ascii=False)
        else:
            body_text = json.dumps(resolved, ensure_ascii=False, default=str)
        if not _has_header(headers, "Content-Type"):
            headers["Content-Type"] = "application/json; charset=utf-8"
        return body_text.encode("utf-8"), _redact_json_text(body_text)

    if body_type == "form":
        if resolved is None:
            return None, ""
        pairs = _as_pairs(resolved)
        if pairs is None:
            text = str(resolved)
            if not _has_header(headers, "Content-Type"):
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            return text.encode("utf-8"), _mask_text_pairs(text)
        if not _has_header(headers, "Content-Type"):
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        encoded = _encode_form(pairs)
        return encoded.encode("utf-8"), _mask_form_pairs(pairs)

    if body_type == "multipart":
        if resolved is None:
            return None, ""
        pairs = _as_pairs(resolved) or []
        if not _has_header(headers, "Content-Type"):
            headers["Content-Type"] = "multipart/form-data; boundary=%s" % MULTIPART_BOUNDARY
        lines: List[str] = []
        preview_lines: List[str] = []
        for name, value in pairs:
            text = _stringify(value)
            lines.append("--%s" % MULTIPART_BOUNDARY)
            lines.append('Content-Disposition: form-data; name="%s"' % name)
            lines.append("")
            lines.append(text)
            shown = redact.MASK if redact.is_secret_key(name) else text
            preview_lines.append("%s: %s" % (name, shown))
        lines.append("--%s--" % MULTIPART_BOUNDARY)
        lines.append("")
        # 仅支持普通文本字段（不支持文件上传），如需文件请扩展为二进制部件
        return "\r\n".join(lines).encode("utf-8"), _truncate_preview("\n".join(preview_lines))

    # raw（含未知类型降级）
    text = "" if resolved is None else (resolved if isinstance(resolved, str) else _stringify(resolved))
    if not _has_header(headers, "Content-Type"):
        headers["Content-Type"] = "text/plain"
    return text.encode("utf-8"), _mask_text_pairs(_truncate_preview(text))


def _as_pairs(value: Any) -> Optional[List[Tuple[str, Any]]]:
    """把 form/multipart 的 body 归一化成 ``[(name, value), ...]``；无法归一化返回 ``None``。"""
    if isinstance(value, Mapping):
        return [(str(key), item) for key, item in value.items()]
    if isinstance(value, (list, tuple)):
        pairs: List[Tuple[str, Any]] = []
        for item in value:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                pairs.append((str(item[0]), item[1]))
            else:
                pairs.append((str(item), ""))
        return pairs
    return None


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:  # pragma: no cover - 极端对象兜底
        return str(value)


def _parses_as_json(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except Exception:
        return False


def _truncate_preview(text: str) -> str:
    if len(text) <= PREVIEW_BODY_LIMIT:
        return text
    return text[:PREVIEW_BODY_LIMIT] + "\n…（预览已截断）"


def _redact_json_text(text: str) -> str:
    """JSON 体预览：按字段名递归脱敏后重新序列化。"""
    try:
        data = json.loads(text)
    except Exception:
        return _mask_text_pairs(_truncate_preview(text))
    if isinstance(data, (dict, list)):
        masked = redact.redact_mapping(data)
        return _truncate_preview(json.dumps(masked, ensure_ascii=False, indent=2))
    return _truncate_preview(text)


def _mask_form_pairs(pairs: Sequence[Tuple[str, Any]]) -> str:
    """表单体预览：与真实请求体同构，仅把敏感字段的值换成掩码。"""
    return _truncate_preview(_mask_text_pairs(_encode_form(pairs)))


def _encode_form(pairs: Sequence[Tuple[str, Any]]) -> str:
    """表单编码：标准 ``application/x-www-form-urlencoded``（空格转 ``+``）。"""
    return urlencode([(name, _stringify(value)) for name, value in pairs], doseq=True)


def _mask_text_pairs(text: str) -> str:
    """文本体预览：对 ``"key": value`` 与 ``key=value`` 形式的敏感字段做掩码。"""

    def replace_json(match: "re.Match[str]") -> str:
        if redact.is_secret_key(match.group("jkey")):
            return '%s"%s"' % (match.group(1), redact.MASK)
        return match.group(0)

    def replace_form(match: "re.Match[str]") -> str:
        if redact.is_secret_key(match.group("fkey")):
            return "%s=%s" % (match.group("fkey"), redact.MASK)
        return match.group(0)

    return _FORM_PAIR_RE.sub(replace_form, _JSON_PAIR_RE.sub(replace_json, text or ""))
