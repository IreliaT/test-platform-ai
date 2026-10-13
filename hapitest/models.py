"""平台规范化数据模型（canonical model）。

所有导入器（OpenAPI / Postman / Apifox / curl / Spring 扫描）、执行引擎、
Web API 与报告都只使用本文件的字段名，禁止各自发明字段。

序列化约定：
- 对外 JSON 一律 snake_case；
- 每个模型提供 ``to_dict()`` 与 ``from_dict()``，``from_dict`` 必须容忍缺失字段和多余字段。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
BODY_TYPES = ("none", "json", "form", "multipart", "raw")
AUTH_TYPES = ("none", "inherit", "bearer", "basic", "api_key", "header", "aliyun_sls")
PARAM_LOCATIONS = ("path", "query", "header", "cookie", "body", "form")
CASE_SOURCES = ("manual", "openapi", "postman", "apifox", "curl", "spring", "template")

# 用例终态遵循“缺条件不算通过”原则：
#   passed     断言全部通过
#   failed     至少一条断言失败
#   error      请求未完成（连接失败、超时、地址被拦截）
#   incomplete 缺少执行条件或未执行（未配置 base_url、无断言、前置步骤失败）
#   skipped    被显式跳过（用例停用、套件提前终止）
CASE_STATUSES = ("passed", "failed", "error", "incomplete", "skipped")

RUN_STATUSES = ("queued", "running", "passed", "failed", "error", "incomplete", "canceled")

# 进程退出码：仅 passed 为 0；其余非 0，保证 CI 不会把“没跑成”当成功
EXIT_CODES = {"passed": 0, "failed": 1, "error": 2, "incomplete": 2, "skipped": 2, "canceled": 2}
RUN_TRIGGERS = ("manual", "cli", "api", "schedule", "ci")
PRIORITIES = ("P0", "P1", "P2", "P3")

# 断言目标：target 描述“从哪里取值”
#   status_code | body_text | json | header:<Name> | cookie:<Name> | duration_ms | variable:<name>
ASSERTION_TYPES = (
    "status_code",
    "body_text",
    "json_path",
    "header",
    "cookie",
    "response_time",
    "json_schema",
    "variable",
)

# 操作符：所有断言类型共用同一套（不适用的组合由校验器拒绝）
OPERATORS = (
    "eq",
    "ne",
    "gt",
    "gte",
    "lt",
    "lte",
    "contains",
    "not_contains",
    "starts_with",
    "ends_with",
    "regex",
    "in",
    "not_in",
    "exists",
    "not_exists",
    "is_true",
    "is_false",
    "is_empty",
    "not_empty",
    "type_is",
    "length_eq",
    "length_gt",
    "length_lt",
    "between",
    "approx",
    "json_contains",
    "json_equals",
)

EXTRACTOR_SOURCES = ("json_path", "json_path_all", "header", "cookie", "regex", "body_text", "status_code", "duration_ms")
EXTRACTOR_SCOPES = ("run", "case")


def now_iso() -> str:
    """统一的 UTC ISO8601（秒精度，带 Z）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def epoch_ms() -> int:
    return int(time.time() * 1000)


def new_id(prefix: str) -> str:
    return "%s_%s" % (prefix, uuid.uuid4().hex[:12])


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _as_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _str_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item is not None and str(item) != ""]
    return [str(value)]


@dataclass
class KeyValue:
    """query / header / cookie / form 字段的统一表示。"""

    name: str = ""
    value: str = ""
    enabled: bool = True
    description: str = ""
    required: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "enabled": bool(self.enabled),
            "description": self.description,
            "required": bool(self.required),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "KeyValue":
        if isinstance(data, (list, tuple)) and len(data) == 2:
            return cls(name=str(data[0]), value="" if data[1] is None else str(data[1]))
        if not isinstance(data, dict):
            return cls(name="" if data is None else str(data))
        return cls(
            name=str(data.get("name", "")),
            value="" if data.get("value") is None else str(data.get("value")),
            enabled=_as_bool(data.get("enabled"), True),
            description=str(data.get("description", "") or ""),
            required=_as_bool(data.get("required"), False),
        )


@dataclass
class AuthSpec:
    """认证配置，可继承环境级认证。"""

    type: str = "inherit"
    token: str = ""
    username: str = ""
    password: str = ""
    key: str = ""
    value: str = ""
    location: str = "header"  # api_key 放置位置：header | query
    scheme: str = "Bearer"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "token": self.token,
            "username": self.username,
            "password": self.password,
            "key": self.key,
            "value": self.value,
            "location": self.location,
            "scheme": self.scheme,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "AuthSpec":
        if not isinstance(data, dict):
            return cls()
        auth_type = str(data.get("type", "inherit") or "inherit")
        if auth_type not in AUTH_TYPES:
            auth_type = "inherit"
        return cls(
            type=auth_type,
            token=str(data.get("token", "") or ""),
            username=str(data.get("username", "") or ""),
            password=str(data.get("password", "") or ""),
            key=str(data.get("key", "") or ""),
            value=str(data.get("value", "") or ""),
            location=str(data.get("location", "header") or "header"),
            scheme=str(data.get("scheme", "Bearer") or "Bearer"),
        )


@dataclass
class RequestSpec:
    """一次 HTTP 请求的完整描述；用例与接口模板共用。"""

    method: str = "GET"
    path: str = "/"
    base_url: Optional[str] = None
    query: List[KeyValue] = field(default_factory=list)
    headers: List[KeyValue] = field(default_factory=list)
    cookies: List[KeyValue] = field(default_factory=list)
    body_type: str = "none"
    body: Any = None
    auth: AuthSpec = field(default_factory=AuthSpec)
    timeout_ms: Optional[int] = None
    follow_redirects: bool = True
    verify_tls: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "method": str(self.method or "GET").upper(),
            "path": self.path,
            "base_url": self.base_url,
            "query": [item.to_dict() for item in self.query],
            "headers": [item.to_dict() for item in self.headers],
            "cookies": [item.to_dict() for item in self.cookies],
            "body_type": self.body_type,
            "body": self.body,
            "auth": self.auth.to_dict(),
            "timeout_ms": self.timeout_ms,
            "follow_redirects": bool(self.follow_redirects),
            "verify_tls": bool(self.verify_tls),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "RequestSpec":
        if not isinstance(data, dict):
            return cls()
        method = str(data.get("method", "GET") or "GET").upper()
        if method not in HTTP_METHODS:
            method = "GET"
        body_type = str(data.get("body_type", "none") or "none")
        if body_type not in BODY_TYPES:
            body_type = "raw"
        return cls(
            method=method,
            path=str(data.get("path", "/") or "/"),
            base_url=(str(data["base_url"]) if data.get("base_url") else None),
            query=[KeyValue.from_dict(item) for item in (data.get("query") or [])],
            headers=[KeyValue.from_dict(item) for item in (data.get("headers") or [])],
            cookies=[KeyValue.from_dict(item) for item in (data.get("cookies") or [])],
            body_type=body_type,
            body=data.get("body"),
            auth=AuthSpec.from_dict(data.get("auth")),
            timeout_ms=_as_int(data.get("timeout_ms"), None),
            follow_redirects=_as_bool(data.get("follow_redirects"), True),
            verify_tls=_as_bool(data.get("verify_tls"), True),
        )


@dataclass
class ParamSpec:
    """接口参数元数据：用于界面生成表单、导入时保留契约信息。"""

    name: str = ""
    location: str = "query"
    required: bool = False
    type: str = "string"
    example: Any = None
    default: Any = None
    description: str = ""
    enum_values: List[Any] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "location": self.location,
            "required": bool(self.required),
            "type": self.type,
            "example": self.example,
            "default": self.default,
            "description": self.description,
            "enum_values": list(self.enum_values),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ParamSpec":
        if not isinstance(data, dict):
            return cls(name="" if data is None else str(data))
        location = str(data.get("location", "query") or "query")
        if location not in PARAM_LOCATIONS:
            location = "query"
        return cls(
            name=str(data.get("name", "")),
            location=location,
            required=_as_bool(data.get("required"), False),
            type=str(data.get("type", "string") or "string"),
            example=data.get("example"),
            default=data.get("default"),
            description=str(data.get("description", "") or ""),
            enum_values=list(data.get("enum_values") or []),
        )


@dataclass
class AssertionSpec:
    """一条断言。"""

    type: str = "status_code"
    operator: str = "eq"
    expected: Any = None
    path: str = ""
    name: str = ""
    tolerance: Optional[float] = None
    message: str = ""
    enabled: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "operator": self.operator,
            "expected": self.expected,
            "path": self.path,
            "name": self.name,
            "tolerance": self.tolerance,
            "message": self.message,
            "enabled": bool(self.enabled),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "AssertionSpec":
        if not isinstance(data, dict):
            return cls()
        return cls(
            type=str(data.get("type", "status_code") or "status_code"),
            operator=str(data.get("operator", "eq") or "eq"),
            expected=data.get("expected"),
            path=str(data.get("path", "") or ""),
            name=str(data.get("name", "") or ""),
            tolerance=(None if data.get("tolerance") in (None, "") else float(data["tolerance"])),
            message=str(data.get("message", "") or ""),
            enabled=_as_bool(data.get("enabled"), True),
        )

    def describe(self) -> str:
        target = self.name or self.path or ""
        if self.type == "status_code":
            return "status_code %s %s" % (self.operator, self.expected)
        return "%s[%s] %s %s" % (self.type, target, self.operator, self.expected)


@dataclass
class ExtractorSpec:
    """从响应中提取变量，供后续断言/用例引用。"""

    name: str = ""
    source: str = "json_path"
    expression: str = ""
    default: Any = None
    scope: str = "run"
    required: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "source": self.source,
            "expression": self.expression,
            "default": self.default,
            "scope": self.scope,
            "required": bool(self.required),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ExtractorSpec":
        if not isinstance(data, dict):
            return cls()
        source = str(data.get("source", "json_path") or "json_path")
        if source not in EXTRACTOR_SOURCES:
            source = "body_text"
        scope = str(data.get("scope", "run") or "run")
        if scope not in EXTRACTOR_SCOPES:
            scope = "run"
        return cls(
            name=str(data.get("name", "")),
            source=source,
            expression=str(data.get("expression", "") or ""),
            default=data.get("default"),
            scope=scope,
            required=_as_bool(data.get("required"), False),
        )


@dataclass
class StepSpec:
    """前置/后置步骤（用于登录串联、数据准备）。"""

    name: str = ""
    request: RequestSpec = field(default_factory=RequestSpec)
    extractors: List[ExtractorSpec] = field(default_factory=list)
    assertions: List[AssertionSpec] = field(default_factory=list)
    continue_on_failure: bool = False
    poll_timeout_ms: int = 0
    poll_interval_ms: int = 1000
    poll_read_only: bool = False
    required_variables: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "request": self.request.to_dict(),
            "extractors": [item.to_dict() for item in self.extractors],
            "assertions": [item.to_dict() for item in self.assertions],
            "continue_on_failure": bool(self.continue_on_failure),
            "poll_timeout_ms": self.poll_timeout_ms,
            "poll_interval_ms": self.poll_interval_ms,
            "poll_read_only": self.poll_read_only,
            "required_variables": list(self.required_variables),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "StepSpec":
        if not isinstance(data, dict):
            return cls()
        return cls(
            name=str(data.get("name", "") or ""),
            request=RequestSpec.from_dict(data.get("request")),
            extractors=[ExtractorSpec.from_dict(item) for item in (data.get("extractors") or [])],
            assertions=[AssertionSpec.from_dict(item) for item in (data.get("assertions") or [])],
            continue_on_failure=_as_bool(data.get("continue_on_failure"), False),
            poll_timeout_ms=_as_int(data.get("poll_timeout_ms"), 0),
            poll_interval_ms=_as_int(data.get("poll_interval_ms"), 1000),
            poll_read_only=_as_bool(data.get("poll_read_only"), False),
            required_variables=_str_list(data.get("required_variables")),
        )


@dataclass
class EndpointSpec:
    """一个接口（API）。"""

    id: Optional[int] = None
    name: str = ""
    folder: str = ""
    method: str = "GET"
    path: str = "/"
    summary: str = ""
    description: str = ""
    tags: List[str] = field(default_factory=list)
    params: List[ParamSpec] = field(default_factory=list)
    request: RequestSpec = field(default_factory=RequestSpec)
    source: str = "manual"
    source_ref: str = ""
    operation_id: str = ""
    deprecated: bool = False
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "folder": self.folder,
            "method": self.method,
            "path": self.path,
            "summary": self.summary,
            "description": self.description,
            "tags": list(self.tags),
            "params": [item.to_dict() for item in self.params],
            "request": self.request.to_dict(),
            "source": self.source,
            "source_ref": self.source_ref,
            "operation_id": self.operation_id,
            "deprecated": bool(self.deprecated),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "EndpointSpec":
        if not isinstance(data, dict):
            return cls()
        raw_request = data.get("request") if isinstance(data.get("request"), dict) else {}
        request = RequestSpec.from_dict(raw_request)
        raw_method = str(data.get("method", "") or "").strip().upper()
        raw_path = str(data.get("path", "") or "").strip()
        # 接口级 method/path 是权威值；两者缺一时用 request 中的值补齐，
        # 避免出现 endpoint.path=/x 而 request.path=/ 的不一致状态
        if raw_path and not raw_request.get("path"):
            request.path = raw_path
        if raw_method and not raw_request.get("method"):
            request.method = raw_method
        method = raw_method or request.method
        path = raw_path or request.path
        if not raw_path and request.path:
            path = request.path
        return cls(
            id=_as_int(data.get("id")),
            name=str(data.get("name", "") or ""),
            folder=str(data.get("folder", "") or ""),
            method=method if method in HTTP_METHODS else "GET",
            path=path or "/",
            summary=str(data.get("summary", "") or ""),
            description=str(data.get("description", "") or ""),
            tags=_str_list(data.get("tags")),
            params=[ParamSpec.from_dict(item) for item in (data.get("params") or [])],
            request=request,
            source=str(data.get("source", "manual") or "manual"),
            source_ref=str(data.get("source_ref", "") or ""),
            operation_id=str(data.get("operation_id", "") or ""),
            deprecated=_as_bool(data.get("deprecated"), False),
            created_at=str(data.get("created_at", "") or ""),
            updated_at=str(data.get("updated_at", "") or ""),
        )

    def signature(self) -> str:
        """用于导入时判定“同一接口”的稳定标识。"""
        return "%s %s" % (self.method.upper(), normalize_path(self.path))


@dataclass
class CaseSpec:
    """一条测试用例。endpoint_id 可为空（完全自定义请求）。"""

    id: Optional[int] = None
    name: str = ""
    folder: str = ""
    description: str = ""
    endpoint_id: Optional[int] = None
    request: RequestSpec = field(default_factory=RequestSpec)
    assertions: List[AssertionSpec] = field(default_factory=list)
    extractors: List[ExtractorSpec] = field(default_factory=list)
    setup: List[StepSpec] = field(default_factory=list)
    variables: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)
    priority: str = "P2"
    enabled: bool = True
    retries: int = 0
    source: str = "manual"
    source_ref: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "folder": self.folder,
            "description": self.description,
            "endpoint_id": self.endpoint_id,
            "request": self.request.to_dict(),
            "assertions": [item.to_dict() for item in self.assertions],
            "extractors": [item.to_dict() for item in self.extractors],
            "setup": [item.to_dict() for item in self.setup],
            "variables": dict(self.variables),
            "tags": list(self.tags),
            "priority": self.priority,
            "enabled": bool(self.enabled),
            "retries": int(self.retries or 0),
            "source": self.source,
            "source_ref": self.source_ref,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "CaseSpec":
        if not isinstance(data, dict):
            return cls()
        return cls(
            id=_as_int(data.get("id")),
            name=str(data.get("name", "") or ""),
            folder=str(data.get("folder", "") or ""),
            description=str(data.get("description", "") or ""),
            endpoint_id=_as_int(data.get("endpoint_id")),
            request=RequestSpec.from_dict(data.get("request")),
            assertions=[AssertionSpec.from_dict(item) for item in (data.get("assertions") or [])],
            extractors=[ExtractorSpec.from_dict(item) for item in (data.get("extractors") or [])],
            setup=[StepSpec.from_dict(item) for item in (data.get("setup") or [])],
            variables=dict(data.get("variables") or {}),
            tags=_str_list(data.get("tags")),
            priority=str(data.get("priority", "P2") or "P2"),
            enabled=_as_bool(data.get("enabled"), True),
            retries=int(_as_int(data.get("retries"), 0) or 0),
            source=str(data.get("source", "manual") or "manual"),
            source_ref=str(data.get("source_ref", "") or ""),
            created_at=str(data.get("created_at", "") or ""),
            updated_at=str(data.get("updated_at", "") or ""),
        )


@dataclass
class EnvironmentSpec:
    """环境：一套 base_url 与变量。"""

    id: Optional[int] = None
    name: str = "默认环境"
    base_url: str = ""
    variables: Dict[str, Any] = field(default_factory=dict)
    auth: AuthSpec = field(default_factory=AuthSpec)
    is_default: bool = False
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self, reveal_secrets: bool = False) -> Dict[str, Any]:
        variables = dict(self.variables)
        if not reveal_secrets:
            from .redact import mask_if_secret

            variables = {key: mask_if_secret(key, value) for key, value in variables.items()}
        return {
            "id": self.id,
            "name": self.name,
            "base_url": self.base_url,
            "variables": variables,
            "auth": self.auth.to_dict() if reveal_secrets else _mask_auth(self.auth.to_dict()),
            "is_default": bool(self.is_default),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "EnvironmentSpec":
        if not isinstance(data, dict):
            return cls()
        return cls(
            id=_as_int(data.get("id")),
            name=str(data.get("name", "默认环境") or "默认环境"),
            base_url=str(data.get("base_url", "") or ""),
            variables=dict(data.get("variables") or {}),
            auth=AuthSpec.from_dict(data.get("auth")),
            is_default=_as_bool(data.get("is_default"), False),
            created_at=str(data.get("created_at", "") or ""),
            updated_at=str(data.get("updated_at", "") or ""),
        )


def _mask_auth(auth: Dict[str, Any]) -> Dict[str, Any]:
    from .redact import mask

    masked = dict(auth)
    for key in ("token", "password", "value"):
        if masked.get(key):
            masked[key] = mask(masked[key])
    return masked


@dataclass
class SuiteSpec:
    """用例集合（有序）。"""

    id: Optional[int] = None
    name: str = ""
    description: str = ""
    case_ids: List[int] = field(default_factory=list)
    env_id: Optional[int] = None
    failure_strategy: str = "continue"  # continue | stop
    concurrency: int = 1
    tags: List[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "case_ids": list(self.case_ids),
            "env_id": self.env_id,
            "failure_strategy": self.failure_strategy,
            "concurrency": int(self.concurrency or 1),
            "tags": list(self.tags),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "SuiteSpec":
        if not isinstance(data, dict):
            return cls()
        return cls(
            id=_as_int(data.get("id")),
            name=str(data.get("name", "") or ""),
            description=str(data.get("description", "") or ""),
            case_ids=[int(item) for item in (data.get("case_ids") or []) if str(item).strip() != ""],
            env_id=_as_int(data.get("env_id")),
            failure_strategy=str(data.get("failure_strategy", "continue") or "continue"),
            concurrency=int(_as_int(data.get("concurrency"), 1) or 1),
            tags=_str_list(data.get("tags")),
            created_at=str(data.get("created_at", "") or ""),
            updated_at=str(data.get("updated_at", "") or ""),
        )


@dataclass
class HttpResponse:
    """一次请求的结果（已脱敏，可直接落库）。"""

    ok: bool = False
    status: Optional[int] = None
    headers: Dict[str, str] = field(default_factory=dict)
    body_text: str = ""
    body_json: Any = None
    duration_ms: int = 0
    url: str = ""
    method: str = "GET"
    request_preview: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    truncated: bool = False
    size_bytes: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": bool(self.ok),
            "status": self.status,
            "headers": dict(self.headers),
            "body_text": self.body_text,
            "body_json": self.body_json,
            "duration_ms": int(self.duration_ms or 0),
            "url": self.url,
            "method": self.method,
            "request_preview": dict(self.request_preview),
            "error": self.error,
            "truncated": bool(self.truncated),
            "size_bytes": int(self.size_bytes or 0),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "HttpResponse":
        if not isinstance(data, dict):
            return cls()
        return cls(
            ok=_as_bool(data.get("ok"), False),
            status=_as_int(data.get("status")),
            headers={str(k): str(v) for k, v in dict(data.get("headers") or {}).items()},
            body_text=str(data.get("body_text", "") or ""),
            body_json=data.get("body_json"),
            duration_ms=int(_as_int(data.get("duration_ms"), 0) or 0),
            url=str(data.get("url", "") or ""),
            method=str(data.get("method", "GET") or "GET"),
            request_preview=dict(data.get("request_preview") or {}),
            error=str(data.get("error", "") or ""),
            truncated=_as_bool(data.get("truncated"), False),
            size_bytes=int(_as_int(data.get("size_bytes"), 0) or 0),
        )


@dataclass
class AssertionResult:
    index: int = 0
    type: str = ""
    operator: str = ""
    target: str = ""
    expected: Any = None
    actual: Any = None
    passed: bool = False
    message: str = ""
    describe: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "type": self.type,
            "operator": self.operator,
            "target": self.target,
            "expected": self.expected,
            "actual": self.actual,
            "passed": bool(self.passed),
            "message": self.message,
            "describe": self.describe,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "AssertionResult":
        if not isinstance(data, dict):
            return cls()
        return cls(
            index=int(_as_int(data.get("index"), 0) or 0),
            type=str(data.get("type", "") or ""),
            operator=str(data.get("operator", "") or ""),
            target=str(data.get("target", "") or ""),
            expected=data.get("expected"),
            actual=data.get("actual"),
            passed=_as_bool(data.get("passed"), False),
            message=str(data.get("message", "") or ""),
            describe=str(data.get("describe", "") or ""),
        )


@dataclass
class CaseResult:
    case_id: Optional[int] = None
    case_name: str = ""
    status: str = "skipped"
    duration_ms: int = 0
    started_at: str = ""
    attempts: int = 0
    request: Dict[str, Any] = field(default_factory=dict)
    response: Dict[str, Any] = field(default_factory=dict)
    assertions: List[AssertionResult] = field(default_factory=list)
    extracted: Dict[str, Any] = field(default_factory=dict)
    steps: List[Dict[str, Any]] = field(default_factory=list)
    error: str = ""
    logs: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id,
            "case_name": self.case_name,
            "status": self.status,
            "duration_ms": int(self.duration_ms or 0),
            "started_at": self.started_at,
            "attempts": int(self.attempts or 0),
            "request": dict(self.request),
            "response": dict(self.response),
            "assertions": [item.to_dict() for item in self.assertions],
            "extracted": dict(self.extracted),
            "steps": list(self.steps),
            "error": self.error,
            "logs": list(self.logs),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "CaseResult":
        if not isinstance(data, dict):
            return cls()
        return cls(
            case_id=_as_int(data.get("case_id")),
            case_name=str(data.get("case_name", "") or ""),
            status=str(data.get("status", "skipped") or "skipped"),
            duration_ms=int(_as_int(data.get("duration_ms"), 0) or 0),
            started_at=str(data.get("started_at", "") or ""),
            attempts=int(_as_int(data.get("attempts"), 0) or 0),
            request=dict(data.get("request") or {}),
            response=dict(data.get("response") or {}),
            assertions=[AssertionResult.from_dict(item) for item in (data.get("assertions") or [])],
            extracted=dict(data.get("extracted") or {}),
            steps=list(data.get("steps") or []),
            error=str(data.get("error", "") or ""),
            logs=list(data.get("logs") or []),
        )


@dataclass
class RunResult:
    id: str = ""
    project_id: str = ""
    status: str = "queued"
    trigger: str = "manual"
    suite_id: Optional[int] = None
    suite_name: str = ""
    env_id: Optional[int] = None
    env_name: str = ""
    total: int = 0
    passed: int = 0
    failed: int = 0
    errored: int = 0
    incomplete: int = 0
    skipped: int = 0
    duration_ms: int = 0
    started_at: str = ""
    finished_at: str = ""
    operator: str = ""
    note: str = ""
    request_id: str = ""
    fingerprint: str = ""
    case_results: List[CaseResult] = field(default_factory=list)
    logs: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "passed"

    @property
    def exit_code(self) -> int:
        return EXIT_CODES.get(self.status, 2)

    def recount(self) -> None:
        self.total = len(self.case_results)
        self.passed = sum(1 for item in self.case_results if item.status == "passed")
        self.failed = sum(1 for item in self.case_results if item.status == "failed")
        self.errored = sum(1 for item in self.case_results if item.status == "error")
        self.incomplete = sum(1 for item in self.case_results if item.status == "incomplete")
        self.skipped = sum(1 for item in self.case_results if item.status == "skipped")

    def finalize_status(self) -> str:
        """按“缺条件不算通过”原则推导整体状态。"""
        if self.status == "canceled":
            return self.status
        if self.failed:
            self.status = "failed"
        elif self.errored:
            self.status = "error"
        elif self.incomplete or self.skipped:
            self.status = "incomplete"
        elif self.total and self.passed == self.total:
            self.status = "passed"
        else:
            self.status = "incomplete"
        return self.status

    def to_dict(self, with_cases: bool = True) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "id": self.id,
            "project_id": self.project_id,
            "status": self.status,
            "trigger": self.trigger,
            "suite_id": self.suite_id,
            "suite_name": self.suite_name,
            "env_id": self.env_id,
            "env_name": self.env_name,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "errored": self.errored,
            "incomplete": self.incomplete,
            "skipped": self.skipped,
            "duration_ms": int(self.duration_ms or 0),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "operator": self.operator,
            "note": self.note,
            "request_id": self.request_id,
            "fingerprint": self.fingerprint,
            "logs": list(self.logs),
        }
        if with_cases:
            data["case_results"] = [item.to_dict() for item in self.case_results]
        return data

    @classmethod
    def from_dict(cls, data: Any) -> "RunResult":
        if not isinstance(data, dict):
            return cls()
        run = cls(
            id=str(data.get("id", "") or ""),
            project_id=str(data.get("project_id", "") or ""),
            status=str(data.get("status", "queued") or "queued"),
            trigger=str(data.get("trigger", "manual") or "manual"),
            suite_id=_as_int(data.get("suite_id")),
            suite_name=str(data.get("suite_name", "") or ""),
            env_id=_as_int(data.get("env_id")),
            env_name=str(data.get("env_name", "") or ""),
            total=int(_as_int(data.get("total"), 0) or 0),
            passed=int(_as_int(data.get("passed"), 0) or 0),
            failed=int(_as_int(data.get("failed"), 0) or 0),
            errored=int(_as_int(data.get("errored"), 0) or 0),
            incomplete=int(_as_int(data.get("incomplete"), 0) or 0),
            skipped=int(_as_int(data.get("skipped"), 0) or 0),
            duration_ms=int(_as_int(data.get("duration_ms"), 0) or 0),
            started_at=str(data.get("started_at", "") or ""),
            finished_at=str(data.get("finished_at", "") or ""),
            operator=str(data.get("operator", "") or ""),
            note=str(data.get("note", "") or ""),
            request_id=str(data.get("request_id", "") or ""),
            fingerprint=str(data.get("fingerprint", "") or ""),
            logs=list(data.get("logs") or []),
        )
        run.case_results = [CaseResult.from_dict(item) for item in (data.get("case_results") or [])]
        return run


@dataclass
class ImportStats:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    cases_created: int = 0
    cases_updated: int = 0
    warnings: List[str] = field(default_factory=list)
    source: str = ""
    source_ref: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
            "cases_created": self.cases_created,
            "cases_updated": self.cases_updated,
            "warnings": list(self.warnings),
            "source": self.source,
            "source_ref": self.source_ref,
        }


@dataclass
class ImportResult:
    """导入器的统一返回：待写入的接口 + 可选自动生成用例 + 统计。"""

    endpoints: List[EndpointSpec] = field(default_factory=list)
    cases: List[CaseSpec] = field(default_factory=list)
    stats: ImportStats = field(default_factory=ImportStats)
    source: str = ""
    title: str = ""
    version: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "endpoints": [item.to_dict() for item in self.endpoints],
            "cases": [item.to_dict() for item in self.cases],
            "stats": self.stats.to_dict(),
            "source": self.source,
            "title": self.title,
            "version": self.version,
        }


def normalize_path(path: str) -> str:
    """接口路径归一化：去掉 query、统一前导斜杠、折叠重复斜杠、保留路径参数名。"""
    if not path:
        return "/"
    text = str(path).strip()
    if "://" in text:
        from urllib.parse import urlsplit

        text = urlsplit(text).path or "/"
    text = text.split("?", 1)[0].split("#", 1)[0]
    if not text.startswith("/"):
        text = "/" + text
    while "//" in text:
        text = text.replace("//", "/")
    if len(text) > 1 and text.endswith("/"):
        text = text[:-1]
    return text or "/"


def detect_path_param_names(path: str) -> List[str]:
    """提取 {id} / :id 形式的路径参数名。"""
    import re

    names = re.findall(r"\{([^}/]+)\}", path)
    if not names:
        names = re.findall(r":([A-Za-z_][A-Za-z0-9_]*)", path)
    return [name.strip() for name in names if name.strip()]


def validate_assertion(assertion: AssertionSpec) -> List[str]:
    """返回错误信息列表，空列表表示合法。"""
    errors: List[str] = []
    if assertion.type not in ASSERTION_TYPES:
        errors.append("未知断言类型: %s" % assertion.type)
    if assertion.operator not in OPERATORS:
        errors.append("未知操作符: %s" % assertion.operator)
    if assertion.type in ("json_path", "json_schema") and not assertion.path:
        errors.append("%s 断言必须提供 path" % assertion.type)
    if assertion.type in ("header", "cookie") and not assertion.name:
        errors.append("%s 断言必须提供 name" % assertion.type)
    if assertion.type == "variable" and not assertion.name:
        errors.append("variable 断言必须提供 name")
    if assertion.operator not in ("exists", "not_exists", "is_empty", "not_empty", "is_true", "is_false"):
        if assertion.expected is None and assertion.operator not in ("type_is",):
            errors.append("操作符 %s 需要 expected" % assertion.operator)
    if assertion.operator in ("in", "not_in") and not isinstance(assertion.expected, (list, tuple)):
        errors.append("操作符 %s 的 expected 必须是数组" % assertion.operator)
    if assertion.operator == "between" and not (isinstance(assertion.expected, (list, tuple)) and len(assertion.expected) == 2):
        errors.append("操作符 between 的 expected 必须是 [min, max]")
    return errors


def validate_case(case: CaseSpec) -> List[str]:
    errors: List[str] = []
    if not case.name.strip():
        errors.append("用例名称不能为空")
    if not case.request.path.strip():
        errors.append("请求路径不能为空")
    for assertion in case.assertions:
        errors.extend(validate_assertion(assertion))
    for extractor in case.extractors:
        if not extractor.name:
            errors.append("提取器必须提供变量名")
    return errors
