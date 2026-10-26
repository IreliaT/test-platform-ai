"""敏感信息识别与脱敏。

平台会记录请求与响应，必须保证密码、令牌不落库、不出现在界面和报告里。
规则：变量名/字段名命中敏感词命中即视为敏感；值统一替换为掩码。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional

MASK = "******"

SENSITIVE_KEYWORDS = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "access_key",
    "accesskey",
    "api_key",
    "apikey",
    "app_secret",
    "appsecret",
    "private_key",
    "privatekey",
    "authorization",
    "auth",
    "cookie",
    "session",
    "credential",
    "signature",
    "salt",
)

#: ``sign`` 是短词，按子串匹配会把 assigned / designed / resigned 这类正常字段
#: （例如官方库统计里的 ``assigned`` 计数）误判成敏感字段，因此单独用前缀排除。
_SENSITIVE_SIGN_RE = re.compile(r"(?<!as)(?<!de)(?<!re)sign", re.IGNORECASE)

# 这些 header 名称即使不含关键字也必须脱敏
SENSITIVE_HEADERS = (
    "authorization",
    "cookie",
    "set-cookie",
    "x-auth-token",
    "x-api-key",
    "proxy-authorization",
)

_SENSITIVE_RE = re.compile("|".join(re.escape(k) for k in SENSITIVE_KEYWORDS), re.IGNORECASE)


def is_secret_key(key: Optional[str]) -> bool:
    """字段名或变量名是否敏感。"""
    if not key:
        return False
    text = str(key)
    return bool(_SENSITIVE_RE.search(text) or _SENSITIVE_SIGN_RE.search(text))


def is_secret_header(name: Optional[str]) -> bool:
    if not name:
        return False
    return str(name).strip().lower() in SENSITIVE_HEADERS or is_secret_key(name)


def mask(value: Any) -> str:
    """统一的掩码文本；空值保持可读性。"""
    if value in (None, ""):
        return ""
    return MASK


def mask_if_secret(key: Optional[str], value: Any) -> Any:
    if is_secret_key(key):
        return mask(value)
    return value


def redact_mapping(data: Mapping[str, Any], extra_keys: Iterable[str] = ()) -> Dict[str, Any]:
    """递归脱敏字典/列表结构。"""
    extra = {str(k).lower() for k in extra_keys}
    return _redact(data, extra)


def _redact(value: Any, extra: set) -> Any:
    if isinstance(value, Mapping):
        out: Dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.lower() in extra or is_secret_key(key_text):
                out[key_text] = mask(item)
            else:
                out[key_text] = _redact(item, extra)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact(item, extra) for item in value]
    return value


def redact_headers(headers: Mapping[str, Any]) -> Dict[str, Any]:
    return {str(k): (mask(v) if is_secret_header(str(k)) else v) for k, v in dict(headers).items()}


def redact_url(url: str, sensitive_params: Iterable[str] = ()) -> str:
    """URL 中的敏感 query 参数替换为掩码。"""
    if not url or "?" not in url:
        return url
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    names = {str(n).lower() for n in sensitive_params}
    redacted: List[Any] = []
    for name, value in pairs:
        if name.lower() in names or is_secret_key(name):
            redacted.append((name, MASK))
        else:
            redacted.append((name, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(redacted), parts.fragment))


def secret_names_from_request(request: Mapping[str, Any]) -> List[str]:
    """收集一次请求里出现的敏感变量名，供运行日志脱敏使用。"""
    names: List[str] = []
    for section in ("query", "headers", "cookies"):
        for item in request.get(section) or []:
            if isinstance(item, Mapping) and is_secret_header(str(item.get("name", ""))):
                names.append(str(item.get("name")))
    auth = request.get("auth") or {}
    if isinstance(auth, Mapping):
        for key in ("token", "password", "value"):
            if auth.get(key):
                names.append(key)
    return names
