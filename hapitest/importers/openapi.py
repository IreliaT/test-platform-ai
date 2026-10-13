"""OpenAPI / Swagger 导入器，同时提供各导入器共用的辅助函数。

支持的输入
----------
* OpenAPI 3.0 / 3.1（``openapi: 3.x``）；
* Swagger 2.0（``swagger: "2.0"``，springfox 2.7.0 的输出格式）。

契约要点（与 INTERFACES.md 1.7、平台约定一致）
--------------------------------------------
* 服务器地址不进模型字段：当文档里存在 ``servers[0].url``（3.x）或
  ``schemes[0]://host + basePath``（2.0），且调用方未提供 ``options.base_url`` /
  ``options.server_url`` 时，本模块在 ``stats.warnings`` 里写入一行
  ``detected_server_url: <url>``，由服务端读取后推导 base_url。
  ``ImportResult.title`` / ``version`` 分别填 OpenAPI 的 ``info.title`` / ``info.version``。
* ``request.base_url`` 始终为 ``None``（请求模板只描述 path，base_url 由环境决定）。
* ``request.path`` 已用示例值替换路径参数（``/pets/{petId}`` → ``/pets/7``），
  这样生成的用例可以直接执行；接口模板保留在 ``EndpointSpec.path`` 与 ``params`` 中。
* 路径/查询/头/ Cookie 参数都会生成条目；启用规则：**必填参数启用**，
  **可选参数只要契约里有示例值/默认值就启用**（否则冒烟请求会因缺参数收到 400/500），
  既可选又没有任何示例值时才关闭（``KeyValue.enabled=False``）。
* form / multipart / raw 请求体额外补 ``Content-Type`` 头；json 交给 http_client 默认补。
* 敏感字段名（``redact.is_secret_key``）不复制契约里的凭据：值改写为 ``${参数名}`` 占位并写入 warning，
  在环境变量里定义同名变量即可跑通，不需要再改用例。
* 示例生成完全确定：不使用随机数、时间或环境信息。

共用辅助函数（供 postman / apifox / curl / spring 复用）
------------------------------------------------------
``opt`` / ``WarningCollector`` / ``build_case`` / ``generate_cases`` /
``default_assertions_for_case`` / ``fallback_assertions`` / ``sample_by_type`` /
``apply_default_headers`` / ``join_folder``。
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..models import (
    AssertionSpec,
    CaseSpec,
    EndpointSpec,
    ImportResult,
    ImportStats,
    KeyValue,
    ParamSpec,
    RequestSpec,
)
from ..redact import is_secret_header, is_secret_key, mask

__all__ = [
    "parse_openapi",
    "opt",
    "WarningCollector",
    "build_case",
    "generate_cases",
    "default_assertions_for_case",
    "fallback_assertions",
    "sample_by_type",
    "value_to_text",
    "apply_default_headers",
    "join_folder",
    "HTTP_METHOD_KEYS",
]

HTTP_METHOD_KEYS = ("get", "post", "put", "patch", "delete", "head", "options")

MAX_EXAMPLE_DEPTH = 3
MAX_REF_DEPTH = 8

# 媒体类型优先级（父需求）：json → *+json → form → multipart → text/plain
_MEDIA_PREFERENCE = (
    "application/json",
    "application/x-www-form-urlencoded",
    "multipart/form-data",
    "text/plain",
)

_FIXED_UUID = "123e4567-e89b-12d3-a456-426614174000"

_SAMPLES_BY_FORMAT = {
    "date": "2024-01-01",
    "date-time": "2024-01-01T00:00:00Z",
    "time": "00:00:00",
    "uuid": _FIXED_UUID,
    "email": "user@example.com",
    "idn-email": "user@example.com",
    "uri": "https://example.com/callback",
    "url": "https://example.com/callback",
    "hostname": "example.com",
    "ipv4": "127.0.0.1",
    "ipv6": "::1",
    "byte": "",
    "binary": "",
    "password": mask("password"),
}


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------


def opt(options: Any, name: str, default: Any = None) -> Any:
    """安全读取 ImportOptions（兼容对象与 dict，缺字段时取默认值）。"""
    if options is None:
        return default
    if isinstance(options, Mapping):
        value = options.get(name, default)
    else:
        value = getattr(options, name, default)
    return default if value is None else value


class WarningCollector:
    """按出现顺序收集 warning，自动去重（保证 deterministic）。"""

    def __init__(self) -> None:
        self.items: List[str] = []
        self._seen = set()

    def add(self, message: Any) -> None:
        text = str(message).strip()
        if not text or text in self._seen:
            return
        self._seen.add(text)
        self.items.append(text)

    def extend(self, messages: Sequence[Any]) -> None:
        for item in messages:
            self.add(item)

    def __len__(self) -> int:
        return len(self.items)


def as_mapping(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def text_of(value: Any) -> str:
    return "" if value is None else str(value)


def value_to_text(value: Any) -> str:
    """把参数值转成请求里使用的文本（bool 用小写 true/false，与 HTTP 习惯一致）。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def join_folder(prefix: Any, name: Any) -> str:
    """拼接目录：``prefix`` 与 ``name``（name 可含多级 ``a/b``）。"""
    head = text_of(prefix).strip().strip("/")
    tail = text_of(name).strip().strip("/")
    if head and tail:
        return "%s/%s" % (head, tail)
    return head or tail


def parse_header_lines(lines: Any) -> List[KeyValue]:
    """把 ``"Name: value"`` 形式的默认头解析成 KeyValue 列表。"""
    result: List[KeyValue] = []
    for item in as_list(lines):
        raw = text_of(item)
        if ":" not in raw:
            continue
        name, _, value = raw.partition(":")
        name = name.strip()
        if not name:
            continue
        result.append(KeyValue(name=name, value=value.strip(), enabled=True))
    return result


def apply_default_headers(request: RequestSpec, options: Any) -> None:
    """把 ``options.default_headers`` 追加到请求头（同名时用户配置优先）。"""
    existing = {item.name.lower() for item in request.headers}
    for item in parse_header_lines(opt(options, "default_headers", [])):
        if item.name.lower() in existing:
            continue
        request.headers.append(item)
        existing.add(item.name.lower())


def sample_by_type(type_name: Any, format_name: Any = "") -> Any:
    """按 JSON 类型 / format 生成确定性示例值。"""
    fmt = text_of(format_name).strip().lower()
    if fmt and fmt in _SAMPLES_BY_FORMAT:
        return _SAMPLES_BY_FORMAT[fmt]
    kind = _first_type({"type": type_name})
    if kind == "string":
        return ""
    if kind in ("integer", "number"):
        return 0
    if kind == "boolean":
        return False
    if kind == "array":
        return []
    if kind == "object":
        return {}
    return ""


def _first_type(node: Mapping[str, Any]) -> str:
    """取 schema 的 JSON 类型；OpenAPI 3.1 允许数组（取第一个非 null）。"""
    value = node.get("type") if isinstance(node, Mapping) else None
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str) and item != "null":
                return item
        return "null" if value else ""
    if isinstance(value, str):
        return value
    if isinstance(node, Mapping):
        if "properties" in node or "additionalProperties" in node:
            return "object"
        if "items" in node:
            return "array"
    return ""


def _required_names(node: Mapping[str, Any]) -> List[str]:
    """``required`` 是父对象上的字段名数组（子属性上的布尔值非法，忽略）。"""
    value = node.get("required") if isinstance(node, Mapping) else None
    if isinstance(value, bool) or value is None:
        return []
    names: List[str] = []
    for item in as_list(value):
        if isinstance(item, str) and item and item not in names:
            names.append(item)
    return names


def _enum_values(node: Mapping[str, Any]) -> List[Any]:
    value = node.get("enum") if isinstance(node, Mapping) else None
    return list(value) if isinstance(value, (list, tuple)) else []


def _has_literal(node: Mapping[str, Any]) -> bool:
    """是否带显式示例（example / examples / const / default / enum）。"""
    if not isinstance(node, Mapping):
        return False
    for key in ("example", "const"):
        if key in node and node[key] is not None:
            return True
    if "examples" in node:
        value = node["examples"]
        if isinstance(value, (list, tuple)) and value:
            return True
        if isinstance(value, Mapping) and value:
            return True
    if node.get("default") is not None:
        return True
    return bool(_enum_values(node))


def _literal_value(node: Mapping[str, Any]) -> Any:
    """按 example → examples → const → default → enum[0] 取值。"""
    if not isinstance(node, Mapping):
        return None
    if node.get("example") is not None:
        return copy.deepcopy(node["example"])
    examples = node.get("examples")
    if isinstance(examples, (list, tuple)) and examples:
        return copy.deepcopy(examples[0])
    if isinstance(examples, Mapping) and examples:
        first = list(examples.values())[0]
        if isinstance(first, Mapping) and "value" in first:
            return copy.deepcopy(first["value"])
        return copy.deepcopy(first)
    if node.get("const") is not None:
        return copy.deepcopy(node["const"])
    if node.get("default") is not None:
        return copy.deepcopy(node["default"])
    enums = _enum_values(node)
    if enums:
        return copy.deepcopy(enums[0])
    return None


# ---------------------------------------------------------------------------
# 默认断言（延迟导入 ..assertions，避免并行开发时互相阻塞）
# ---------------------------------------------------------------------------


def fallback_assertions(method: str = "GET", response_schema: Any = None) -> List[AssertionSpec]:
    """最小的默认断言：状态码 + 响应时间（assertions.py 不可用时的兜底）。"""
    return [
        AssertionSpec(
            type="status_code",
            operator="in",
            expected=[200, 201, 204],
            message="状态码在 200/201/204 之内",
        ),
        AssertionSpec(
            type="response_time",
            operator="lt",
            expected=3000,
            message="响应时间小于 3000 毫秒",
        ),
    ]


def default_assertions_for_case(method: str, response_schema: Any = None) -> List[AssertionSpec]:
    """构造默认断言：优先用 ``hapitest.assertions.default_assertions_for``。"""
    try:
        from ..assertions import default_assertions_for  # 并行开发：延迟导入
    except Exception:
        return fallback_assertions(method, response_schema)
    try:
        assertions = default_assertions_for(method, response_schema)
    except Exception:
        return fallback_assertions(method, response_schema)
    if not assertions:
        return fallback_assertions(method, response_schema)
    return list(assertions)


# ---------------------------------------------------------------------------
# 用例生成
# ---------------------------------------------------------------------------


def build_case(
    endpoint: EndpointSpec,
    options: Any = None,
    response_schema: Any = None,
    variables: Optional[Dict[str, Any]] = None,
    assertions: Optional[Sequence[AssertionSpec]] = None,
    source_ref: Optional[str] = None,
) -> CaseSpec:
    """由接口模板生成一条冒烟用例（request 深拷贝，互不影响）。"""
    suffix = text_of(opt(options, "case_suffix", " - 冒烟"))
    case = CaseSpec(
        name="%s%s" % (endpoint.name, suffix),
        folder=endpoint.folder,
        description=endpoint.description,
        request=copy.deepcopy(endpoint.request),
        variables=dict(variables or {}),
        tags=list(endpoint.tags),
        priority="P2",
        source=endpoint.source,
        source_ref=endpoint.source_ref if source_ref is None else source_ref,
    )
    if assertions:
        case.assertions = list(assertions)
    else:
        case.assertions = default_assertions_for_case(endpoint.method, response_schema)
    return case


def generate_cases(
    endpoints: Sequence[EndpointSpec],
    options: Any = None,
    response_schemas: Optional[Sequence[Any]] = None,
    variables: Optional[Dict[str, Any]] = None,
    assertions_map: Optional[Sequence[Optional[Sequence[AssertionSpec]]]] = None,
) -> List[CaseSpec]:
    """批量生成冒烟用例；``options.generate_cases`` 为 False 时返回空列表。"""
    if not opt(options, "generate_cases", True):
        return []
    cases: List[CaseSpec] = []
    for index, endpoint in enumerate(endpoints):
        schema = response_schemas[index] if response_schemas and index < len(response_schemas) else None
        extra = assertions_map[index] if assertions_map and index < len(assertions_map) else None
        cases.append(build_case(endpoint, options, schema, variables, extra))
    return cases


# ---------------------------------------------------------------------------
# $ref 解析
# ---------------------------------------------------------------------------


class RefResolver:
    """JSON Pointer 形式的 ``$ref`` 解析器（仅文档内部引用）。"""

    def __init__(self, document: Any, warnings: WarningCollector, max_depth: int = MAX_REF_DEPTH) -> None:
        self.document = document
        self.warnings = warnings
        self.max_depth = max_depth

    def lookup(self, ref: str) -> Any:
        """按 ``#/a/b/c`` 取值；找不到返回 ``None`` 并记 warning。"""
        if not isinstance(ref, str) or not ref.startswith("#"):
            self.warnings.add("不支持外部 $ref: %s" % ref)
            return None
        pointer = ref[1:]
        if pointer in ("", "/"):
            return self.document
        target: Any = self.document
        for raw in pointer.lstrip("/").split("/"):
            token = raw.replace("~1", "/").replace("~0", "~")
            if isinstance(target, Mapping) and token in target:
                target = target[token]
            elif isinstance(target, (list, tuple)) and token.isdigit() and int(token) < len(target):
                target = target[int(token)]
            else:
                self.warnings.add("无法解析 $ref: %s" % ref)
                return None
        return target

    def resolve(self, node: Any, chain: Tuple[str, ...] = ()) -> Tuple[Any, Tuple[str, ...]]:
        """展开 ``$ref`` 链并合并兄弟字段（OpenAPI 3.1 允许 $ref 带兄弟字段）。"""
        current = node
        guard = 0
        while isinstance(current, Mapping) and isinstance(current.get("$ref"), str):
            ref = current["$ref"]
            if ref in chain:
                self.warnings.add("检测到循环 $ref，已断链: %s" % ref)
                return {}, chain
            if len(chain) >= self.max_depth:
                self.warnings.add("$ref 嵌套超过 %d 层，已截断: %s" % (self.max_depth, ref))
                return {}, chain
            target = self.lookup(ref)
            if target is None:
                return {}, chain
            chain = chain + (ref,)
            merged: Dict[str, Any] = dict(target) if isinstance(target, Mapping) else {}
            for key, value in current.items():
                if key != "$ref":
                    merged[key] = value
            current = merged
            guard += 1
            if guard > self.max_depth:
                return {}, chain
        return current, chain


def _merge_schema(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> Dict[str, Any]:
    """合并两个 schema：properties 深合并、required 并集，其余 overlay 覆盖。"""
    merged: Dict[str, Any] = dict(base)
    for key, value in overlay.items():
        if key == "properties" and isinstance(value, Mapping) and isinstance(merged.get("properties"), Mapping):
            props = dict(merged["properties"])
            props.update(value)
            merged["properties"] = props
        elif key == "required":
            names = list(merged.get("required") or [])
            for item in as_list(value):
                if item not in names:
                    names.append(item)
            merged["required"] = names
        else:
            merged[key] = value
    return merged


def normalize_schema(
    node: Any,
    resolver: RefResolver,
    warnings: WarningCollector,
    depth: int = 0,
    chain: Tuple[str, ...] = (),
) -> Tuple[Dict[str, Any], Tuple[str, ...]]:
    """展开 $ref、合并 allOf、oneOf/anyOf 取第一分支，得到可直接使用的 schema。"""
    resolved, chain = resolver.resolve(node, chain)
    if not isinstance(resolved, Mapping):
        return {}, chain
    if depth > 6:
        return dict(resolved), chain
    if "allOf" in resolved:
        merged: Dict[str, Any] = {}
        for branch in as_list(resolved.get("allOf")):
            branch_node, chain = normalize_schema(branch, resolver, warnings, depth + 1, chain)
            merged = _merge_schema(merged, branch_node)
        parent = {key: value for key, value in resolved.items() if key != "allOf"}
        return normalize_schema(_merge_schema(merged, parent), resolver, warnings, depth + 1, chain)
    for key in ("oneOf", "anyOf"):
        if key in resolved:
            branches = as_list(resolved.get(key))
            if branches:
                warnings.add("%s 仅取第一个分支生成示例" % key)
                first, chain = normalize_schema(branches[0], resolver, warnings, depth + 1, chain)
                parent = {k: v for k, v in resolved.items() if k not in ("oneOf", "anyOf")}
                return normalize_schema(_merge_schema(first, parent), resolver, warnings, depth + 1, chain)
    return dict(resolved), chain


def example_from_schema(
    schema: Any,
    resolver: RefResolver,
    warnings: WarningCollector,
    depth: int = 0,
    in_request: bool = True,
    chain: Tuple[str, ...] = (),
    include_optional: bool = False,
) -> Any:
    """按契约规则生成确定性示例值（None 表示无法确定类型）。"""
    if not isinstance(schema, Mapping):
        return None
    node, chain = normalize_schema(schema, resolver, warnings, 0, chain)
    if not node:
        # 循环引用被截断或引用无法解析：按空对象处理
        return {}
    if depth > MAX_EXAMPLE_DEPTH:
        return None

    literal = _literal_value(node)
    if literal is not None:
        return literal

    kind = _first_type(node)
    if kind == "object" or "properties" in node:
        properties = node.get("properties")
        if not isinstance(properties, Mapping):
            return {}
        required = _required_names(node)
        result: Dict[str, Any] = {}
        for name, raw_prop in properties.items():
            prop, _ = normalize_schema(raw_prop, resolver, warnings, 0, chain)
            if not isinstance(prop, Mapping):
                prop = {}
            if in_request and prop.get("readOnly") is True:
                continue
            key = str(name)
            if not (include_optional or key in required or _has_literal(prop)):
                continue
            if is_secret_key(key):
                result[key] = ""
                warnings.add("敏感字段已留空: %s" % key)
                continue
            value = example_from_schema(prop, resolver, warnings, depth + 1, in_request, chain, include_optional)
            if value is None:
                value = sample_by_type(_first_type(prop), prop.get("format"))
            result[key] = value
        return result
    if kind == "array":
        items = node.get("items")
        item_value = example_from_schema(items, resolver, warnings, depth + 1, in_request, chain, include_optional)
        if item_value is None:
            item_value = sample_by_type(_first_type(items if isinstance(items, Mapping) else {}))
        return [item_value]
    return sample_by_type(kind, node.get("format"))


# ---------------------------------------------------------------------------
# 媒体类型
# ---------------------------------------------------------------------------


def pick_media_type(content: Any) -> Tuple[str, Dict[str, Any]]:
    """按优先级选择媒体类型，返回 (media_type, media_object)。"""
    if not isinstance(content, Mapping) or not content:
        return "", {}
    lowered = {str(key).strip().lower(): value for key, value in content.items()}
    for preferred in _MEDIA_PREFERENCE:
        if preferred in lowered:
            return preferred, as_mapping(lowered[preferred])
    for media_type, value in content.items():
        if str(media_type).strip().lower().endswith("+json"):
            return str(media_type).strip().lower(), as_mapping(value)
    first_key = next(iter(content.keys()))
    return str(first_key).strip().lower(), as_mapping(content[first_key])


def body_type_for_media(media_type: str) -> str:
    kind = text_of(media_type).split(";")[0].strip().lower()
    if not kind:
        return "none"
    if kind in ("application/json", "text/json") or kind.endswith("+json"):
        return "json"
    if kind == "application/x-www-form-urlencoded":
        return "form"
    if kind.startswith("multipart/form-data"):
        return "multipart"
    return "raw"


def content_type_for(body_type: str, media_type: str) -> str:
    """form / multipart / raw 需要显式 Content-Type；json 交给 http_client 默认值。"""
    kind = text_of(media_type).split(";")[0].strip()
    if body_type == "json":
        return ""
    if body_type == "form":
        return kind or "application/x-www-form-urlencoded"
    if body_type == "multipart":
        return kind or "multipart/form-data"
    if body_type == "raw":
        return kind or "text/plain"
    return ""


# ---------------------------------------------------------------------------
# 参数与请求模板
# ---------------------------------------------------------------------------


def _param_schema(node: Mapping[str, Any], is_swagger2: bool) -> Dict[str, Any]:
    """取参数的类型信息：OAS3 用 schema/content，Swagger 2.0 直接内联。"""
    schema = node.get("schema")
    if isinstance(schema, Mapping):
        return dict(schema)
    content = node.get("content")
    if isinstance(content, Mapping) and content:
        _, media = pick_media_type(content)
        inner = media.get("schema")
        if isinstance(inner, Mapping):
            return dict(inner)
    if is_swagger2 or any(key in node for key in ("type", "format", "items", "enum")):
        inline = {key: node[key] for key in ("type", "format", "items", "enum", "default") if key in node}
        if node.get("example") is not None:
            inline["example"] = node["example"]
        if node.get("x-example") is not None:
            inline.setdefault("example", node["x-example"])
        return inline
    return {}


def _param_placeholder(node: Mapping[str, Any], schema: Mapping[str, Any], kind: str) -> Any:
    """example → default → enum[0] → 按类型/format 采样。"""
    literal = _literal_value(node)
    if literal is None:
        literal = _literal_value(schema)
    if literal is not None:
        return literal
    if kind == "path":
        value = sample_by_type(_first_type(schema), schema.get("format"))
        return value if value not in ("", None) else "1"
    return sample_by_type(_first_type(schema), schema.get("format"))


def _collect_parameters(
    raw_parameters: Any,
    resolver: RefResolver,
    warnings: WarningCollector,
) -> List[Dict[str, Any]]:
    """解析参数列表（可含 $ref），保持声明顺序。"""
    result: List[Dict[str, Any]] = []
    for raw in as_list(raw_parameters):
        node, _ = resolver.resolve(raw)
        if not isinstance(node, Mapping):
            warnings.add("参数定义无法解析，已跳过")
            continue
        result.append(dict(node))
    return result


def merge_parameters(path_level: Sequence[Any], operation_level: Sequence[Any]) -> List[Dict[str, Any]]:
    """合并路径级与操作级参数，键为 ``(name, in)``，操作级优先且保持原位置。"""
    merged: List[Dict[str, Any]] = []
    index: Dict[Tuple[str, str], int] = {}
    for node in list(path_level) + list(operation_level):
        if not isinstance(node, Mapping):
            continue
        name = text_of(node.get("name"))
        location = text_of(node.get("in") or "query").lower()
        if not name:
            continue
        key = (name, location)
        if key in index:
            merged[index[key]] = dict(node)
        else:
            index[key] = len(merged)
            merged.append(dict(node))
    return merged


def _body_params(schema: Any, resolver: RefResolver, warnings: WarningCollector) -> List[ParamSpec]:
    """请求体顶层属性 → ``location="body"`` 的参数元数据（readOnly 字段排除）。"""
    node, _ = normalize_schema(schema, resolver, warnings)
    properties = node.get("properties") if isinstance(node, Mapping) else None
    if not isinstance(properties, Mapping):
        return []
    required = _required_names(node)
    params: List[ParamSpec] = []
    for name, raw_prop in properties.items():
        prop, _ = normalize_schema(raw_prop, resolver, warnings)
        if not isinstance(prop, Mapping):
            prop = {}
        if prop.get("readOnly") is True:
            continue
        key = str(name)
        params.append(
            ParamSpec(
                name=key,
                location="body",
                required=key in required,
                type=_first_type(prop) or "string",
                example=_literal_value(prop) if _has_literal(prop) else None,
                default=prop.get("default"),
                description=text_of(prop.get("description")),
                enum_values=_enum_values(prop),
            )
        )
    return params


def _substitute_path(path: str, values: Mapping[str, str]) -> str:
    def replace(match: "re.Match[str]") -> str:
        name = match.group(1)
        return values.get(name, match.group(0))

    return re.sub(r"\{([^}/]+)\}", replace, path)


def _detect_server_url(document: Mapping[str, Any], is_swagger2: bool, warnings: WarningCollector) -> str:
    if is_swagger2:
        host = text_of(document.get("host")).strip()
        if not host:
            return ""
        schemes = [text_of(item) for item in as_list(document.get("schemes")) if text_of(item)]
        scheme = schemes[0] if schemes else "http"
        base_path = text_of(document.get("basePath")).strip()
        if base_path and not base_path.startswith("/"):
            base_path = "/" + base_path
        return "%s://%s%s" % (scheme, host, "" if base_path == "/" else base_path)
    servers = as_list(document.get("servers"))
    for server in servers:
        if not isinstance(server, Mapping):
            continue
        url = text_of(server.get("url")).strip()
        if not url:
            continue
        variables = server.get("variables")
        if isinstance(variables, Mapping):
            for name, spec in variables.items():
                if isinstance(spec, Mapping) and spec.get("default") is not None:
                    url = url.replace("{%s}" % name, text_of(spec["default"]))
        return url
    return ""


def _response_schema(operation: Mapping[str, Any], resolver: RefResolver, is_swagger2: bool) -> Any:
    """取第一个 2xx 响应的 JSON schema（用于生成断言）。"""
    responses = operation.get("responses")
    if not isinstance(responses, Mapping):
        return None
    candidates: List[Tuple[int, Any]] = []
    for key, value in responses.items():
        label = text_of(key).strip()
        if not label.startswith("2"):
            continue
        order = int(label) if label.isdigit() else 299
        candidates.append((order, value))
    candidates.sort(key=lambda item: item[0])
    for _, raw in candidates:
        node, _ = resolver.resolve(raw)
        if not isinstance(node, Mapping):
            continue
        if is_swagger2:
            schema = node.get("schema")
            if isinstance(schema, Mapping):
                return _deep_resolve(schema, resolver)
            continue
        content = node.get("content")
        if not isinstance(content, Mapping) or not content:
            continue
        media_type, media = pick_media_type(content)
        if body_type_for_media(media_type) != "json":
            continue
        schema = media.get("schema")
        if isinstance(schema, Mapping):
            return _deep_resolve(schema, resolver)
    return None


def _deep_resolve(node: Any, resolver: RefResolver, depth: int = 0, chain: Tuple[str, ...] = ()) -> Any:
    """有限深度展开 schema 内的 $ref，便于断言模块直接使用（循环处截断为空对象）。"""
    if depth > 3:
        return {}
    resolved, chain = resolver.resolve(node, chain)
    if not isinstance(resolved, Mapping):
        return resolved
    out: Dict[str, Any] = {}
    for key, value in resolved.items():
        if key == "properties" and isinstance(value, Mapping):
            out[key] = {str(k): _deep_resolve(v, resolver, depth + 1, chain) for k, v in value.items()}
        elif key == "items":
            out[key] = _deep_resolve(value, resolver, depth + 1, chain)
        elif key in ("allOf", "oneOf", "anyOf") and isinstance(value, (list, tuple)):
            branches = [_deep_resolve(item, resolver, depth + 1, chain) for item in value]
            out[key] = branches
            if key == "allOf" and branches:
                merged: Dict[str, Any] = {}
                for branch in branches:
                    if isinstance(branch, Mapping):
                        merged = _merge_schema(merged, branch)
                out = _merge_schema(merged, {k: v for k, v in out.items() if k != key})
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _is_swagger2(document: Mapping[str, Any]) -> bool:
    value = document.get("swagger")
    return isinstance(value, str) and value.strip().startswith("2")


def _build_operation_request(
    path: str,
    method: str,
    parameters: Sequence[Mapping[str, Any]],
    operation: Mapping[str, Any],
    document: Mapping[str, Any],
    resolver: RefResolver,
    warnings: WarningCollector,
    options: Any,
    is_swagger2: bool,
) -> Tuple[List[ParamSpec], RequestSpec, Any]:
    """由参数与请求体构造 (params, request, response_schema)。"""
    params: List[ParamSpec] = []
    request = RequestSpec(method=method.upper(), path=path, base_url=None)
    path_values: Dict[str, str] = {}
    body_schema: Any = None
    body_media = ""
    form_params: List[Mapping[str, Any]] = []

    for node in parameters:
        location = text_of(node.get("in") or "query").strip().lower()
        name = text_of(node.get("name")).strip()
        if not name:
            warnings.add("存在缺少 name 的参数，已跳过")
            continue
        if location == "body":
            body_schema = node.get("schema")
            body_media = body_media or _operation_consumes(operation, document, "")
            continue
        if location == "formdata":
            form_params.append(node)
            continue
        if location not in ("path", "query", "header", "cookie"):
            warnings.add("不支持的参数位置 in=%s（%s），已跳过" % (location, name))
            continue
        schema = _param_schema(node, is_swagger2)
        required = bool(node.get("required")) or location == "path"
        placeholder = _param_placeholder(node, schema, location)
        secret = is_secret_key(name)
        # 契约里带示例值（或默认值）说明作者认为这个值是可用的：
        # 即使参数是可选的，冒烟用例也应该带上，否则会得到一堆"缺参数"的 400/500。
        has_example = placeholder not in (None, "") or bool(node.get("example")) or schema.get("example") not in (None, "")
        if secret:
            # 敏感参数不复制凭据，但保留条目并指向一个环境变量：
            # 在环境里定义同名变量即可跑通，不必再改用例。
            warnings.add("敏感参数使用变量占位: %s（请在环境变量中定义 %s）" % (name, name))
        params.append(
            ParamSpec(
                name=name,
                location=location,
                required=required,
                type=_first_type(schema) or "string",
                example=None if secret else placeholder,
                default=schema.get("default", node.get("default")),
                description=text_of(node.get("description")),
                enum_values=_enum_values(schema),
            )
        )
        value_text = ("${%s}" % name) if secret else value_to_text(placeholder)
        enabled = required or (bool(has_example) and not secret) or secret
        if location == "path":
            path_values[name] = value_text or "1"
        elif location == "query":
            request.query.append(KeyValue(name=name, value=value_text, enabled=enabled, required=required,
                                          description=text_of(node.get("description"))))
        elif location == "header":
            request.headers.append(KeyValue(name=name, value=value_text, enabled=enabled, required=required,
                                            description=text_of(node.get("description"))))
        else:
            request.cookies.append(KeyValue(name=name, value=value_text, enabled=enabled, required=required,
                                            description=text_of(node.get("description"))))

    request.path = _substitute_path(path, path_values)

    # --- 请求体
    if form_params:
        body_media = body_media or _operation_consumes(operation, document, "application/x-www-form-urlencoded")
        properties: Dict[str, Any] = {}
        required_names: List[str] = []
        for node in form_params:
            name = text_of(node.get("name")).strip()
            if not name:
                continue
            schema = _param_schema(node, True)
            properties[name] = schema
            if node.get("required"):
                required_names.append(name)
        body_schema = {"type": "object", "properties": properties, "required": required_names}
    elif body_schema is None:
        raw_body = operation.get("requestBody")
        if isinstance(raw_body, Mapping):
            body_node, _ = resolver.resolve(raw_body)
            if isinstance(body_node, Mapping):
                content = body_node.get("content")
                body_media, media = pick_media_type(content)
                body_schema = media.get("schema")

    if body_schema is not None:
        body_type = body_type_for_media(body_media)
        if not body_media:
            body_media = "application/json" if not form_params else "application/x-www-form-urlencoded"
            body_type = body_type_for_media(body_media)
        request.body_type = body_type
        example = example_from_schema(
            body_schema, resolver, warnings, 0, True, (), body_type in ("form", "multipart")
        )
        if body_type == "json":
            request.body = example if example is not None else {}
        elif body_type in ("form", "multipart"):
            request.body = example if isinstance(example, Mapping) else {}
            for name in request.body:
                if is_secret_key(name):
                    warnings.add("敏感字段已留空: %s" % name)
        else:
            if example is None:
                request.body = ""
            elif isinstance(example, str):
                request.body = example
            else:
                request.body = json.dumps(example, ensure_ascii=False)
        header = content_type_for(body_type, body_media)
        if header:
            request.headers.append(KeyValue(name="Content-Type", value=header, enabled=True))
        params.extend(_body_params(body_schema, resolver, warnings))

    response_schema = _response_schema(operation, resolver, is_swagger2)
    return params, request, response_schema


def _operation_consumes(operation: Mapping[str, Any], document: Mapping[str, Any], fallback: str) -> str:
    for source in (operation, document):
        for item in as_list(source.get("consumes")):
            value = text_of(item).strip()
            if value:
                return value
    return fallback


def parse_openapi(document: Any, source_ref: str = "", options: Any = None) -> ImportResult:
    """解析 OpenAPI 3.0/3.1 或 Swagger 2.0 文档。"""
    if not isinstance(document, Mapping):
        raise ValueError("OpenAPI 文档必须是一个对象（mapping），实际为 %s" % type(document).__name__)
    warnings = WarningCollector()
    is_swagger2 = _is_swagger2(document)
    source = "swagger2" if is_swagger2 else "openapi"
    info = as_mapping(document.get("info"))
    result = ImportResult(source=source, title=text_of(info.get("title")), version=text_of(info.get("version")))
    result.stats = ImportStats(source=source, source_ref=text_of(source_ref))
    resolver = RefResolver(document, warnings)

    server_url = _detect_server_url(document, is_swagger2, warnings)
    if server_url and not text_of(opt(options, "base_url", "")) and not text_of(opt(options, "server_url", "")):
        warnings.add("detected_server_url: %s" % server_url)

    extra_tags = [text_of(item) for item in as_list(opt(options, "tags", [])) if text_of(item)]
    folder_prefix = opt(options, "folder_prefix", "")
    include_deprecated = bool(opt(options, "include_deprecated", False))

    response_schemas: List[Any] = []
    paths = document.get("paths")
    if isinstance(paths, Mapping):
        for raw_path, raw_item in paths.items():
            path = text_of(raw_path).strip() or "/"
            item, _ = resolver.resolve(raw_item)
            if not isinstance(item, Mapping):
                warnings.add("路径项无法解析: %s" % path)
                continue
            path_level = _collect_parameters(item.get("parameters"), resolver, warnings)
            for method in HTTP_METHOD_KEYS:
                raw_operation = item.get(method)
                if not isinstance(raw_operation, Mapping):
                    continue
                operation, _ = resolver.resolve(raw_operation)
                if not isinstance(operation, Mapping):
                    warnings.add("操作无法解析: %s %s" % (method.upper(), path))
                    continue
                deprecated = operation.get("deprecated") is True
                if deprecated and not include_deprecated:
                    result.stats.skipped += 1
                    continue
                operation_level = _collect_parameters(operation.get("parameters"), resolver, warnings)
                merged = merge_parameters(path_level, operation_level)
                params, request, response_schema = _build_operation_request(
                    path, method, merged, operation, document, resolver, warnings, options, is_swagger2
                )
                summary = text_of(operation.get("summary"))
                operation_id = text_of(operation.get("operationId"))
                tags = [text_of(item) for item in as_list(operation.get("tags")) if text_of(item)]
                name = summary or operation_id or "%s %s" % (method.upper(), path)
                folder = join_folder(folder_prefix, tags[0] if tags else "")
                endpoint = EndpointSpec(
                    name=name,
                    folder=folder,
                    method=method.upper(),
                    path=path,
                    summary=summary,
                    description=text_of(operation.get("description")),
                    tags=tags + [item for item in extra_tags if item not in tags],
                    params=params,
                    request=request,
                    source=source,
                    source_ref=text_of(source_ref),
                    operation_id=operation_id,
                    deprecated=deprecated,
                )
                apply_default_headers(endpoint.request, options)
                result.endpoints.append(endpoint)
                response_schemas.append(response_schema)

    cases = generate_cases(result.endpoints, options, response_schemas)
    result.cases = cases
    result.stats.created = len(result.endpoints)
    result.stats.cases_created = len(cases)
    result.stats.warnings = warnings.items
    return result
