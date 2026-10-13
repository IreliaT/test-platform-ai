"""Apifox 导入器。

Apifox 有两种导出形态，本模块都要能处理：

1. **OpenAPI 文档**：检测到顶层 ``openapi`` / ``swagger`` 时直接委托
   :func:`hapitest.importers.openapi.parse_openapi`，随后把 ``source`` 改回 ``apifox``；
2. **Apifox 自有 JSON 导出**：顶层键通常是 ``apiCollection`` / ``apiFolder`` / ``apis`` /
   ``folders`` / ``project`` / ``environments`` / ``schemas``。由于官方格式随版本变化，
   这里采用「宽容遍历」：递归任何列表/字典，凡是同时含 ``path`` 与 ``method``
   （不区分大小写）的节点就当作接口，``name`` / ``title`` 作为接口名或目录名。
   ``{"name": "接口名", "api": {...}}`` 这种包装节点也会被识别。

解析过程永不因为未知结构而崩溃：无法识别的节点记一条 warning 后继续；若整份文档
一个接口都没找到，抛 ``ValueError("无法识别的 Apifox 导出格式")``。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..models import (
    EndpointSpec,
    ImportResult,
    ImportStats,
    KeyValue,
    ParamSpec,
    RequestSpec,
)
from ..redact import is_secret_key
from .openapi import (
    HTTP_METHOD_KEYS,
    WarningCollector,
    apply_default_headers,
    as_list,
    as_mapping,
    body_type_for_media,
    content_type_for,
    generate_cases,
    join_folder,
    opt,
    sample_by_type,
    text_of,
    value_to_text,
)
from .openapi import parse_openapi

__all__ = ["parse_apifox"]

_KNOWN_TOP_KEYS = (
    "apicollection",
    "apifolder",
    "apis",
    "folders",
    "project",
    "environments",
    "schemas",
    "info",
    "variables",
    "globals",
    "globalvariables",
    "globalscripts",
    "apisettings",
    "commonschemas",
    "datamodels",
)

_MEDIA_HINTS = ("type", "contenttype", "mediatype", "content-type")


def _get_ci(node: Mapping[str, Any], *names: str) -> Any:
    """大小写不敏感地读取键。"""
    if not isinstance(node, Mapping):
        return None
    lowered = {str(key).lower(): value for key, value in node.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def _node_name(node: Mapping[str, Any]) -> str:
    return text_of(_get_ci(node, "name", "title", "label", "folderName", "apiName"))


def _is_api(node: Mapping[str, Any]) -> bool:
    path = _get_ci(node, "path", "url")
    method = _get_ci(node, "method")
    if not isinstance(path, str) or not path.strip():
        return False
    if isinstance(method, str):
        return bool(method.strip())
    return False


def _api_like(node: Mapping[str, Any]) -> bool:
    """含 path/method 痕迹但结构不完整的节点，用于产出「跳过」警告。"""
    return any(str(key).lower() in ("path", "method", "url") for key in node.keys())


def _normalize_method(value: Any) -> str:
    method = text_of(value).strip().upper()
    if method in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
        return method
    return ""


def _param_schema(node: Mapping[str, Any]) -> Dict[str, Any]:
    schema = _get_ci(node, "schema")
    if isinstance(schema, Mapping):
        return dict(schema)
    inline = {}
    for key in ("type", "format", "enum", "default", "example"):
        value = _get_ci(node, key)
        if value is not None:
            inline[key] = value
    return inline


def _param_value(node: Mapping[str, Any], schema: Mapping[str, Any]) -> Any:
    """依次从参数自身与 schema 上取 example / default / value / enum[0]。"""
    for source in (node, schema):
        if not isinstance(source, Mapping):
            continue
        for key in ("example", "default", "value"):
            value = _get_ci(source, key)
            if value is not None:
                return value
    for source in (node, schema):
        enum = _get_ci(source, "enum")
        if isinstance(enum, (list, tuple)) and enum:
            return enum[0]
    return None


def _param_entries(raw: Any) -> List[Dict[str, Any]]:
    """把 Apifox 的参数写法统一成 [{'name','in','required',...}]。"""
    entries: List[Dict[str, Any]] = []
    if isinstance(raw, Mapping):
        for location in ("path", "query", "header", "cookie"):
            section = _get_ci(raw, location)
            if section is None:
                continue
            entries.extend(_param_section(section, location))
        if not entries:
            # 可能是 {name: value} 形式
            for key, value in raw.items():
                if isinstance(value, (str, int, float, bool)):
                    entries.append({"name": str(key), "in": "query", "value": value})
        return entries
    for item in as_list(raw):
        if not isinstance(item, Mapping):
            continue
        name = text_of(_get_ci(item, "name", "key"))
        if not name:
            continue
        location = text_of(_get_ci(item, "in", "location", "paramIn") or "query").strip().lower()
        entries.append(
            {
                "name": name,
                "in": "formdata" if location == "formdata" else location,
                "required": bool(_get_ci(item, "required")),
                "description": text_of(_get_ci(item, "description")),
                "schema": _param_schema(item),
                "value": _param_value(item, _param_schema(item)),
                "enabled": not bool(_get_ci(item, "disabled")) and _get_ci(item, "enable") is not False,
            }
        )
    return entries


def _param_section(section: Any, location: str) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    if isinstance(section, Mapping):
        for key, value in section.items():
            if isinstance(value, Mapping):
                name = text_of(_get_ci(value, "name") or key)
                entries.append(
                    {
                        "name": name,
                        "in": location,
                        "required": bool(_get_ci(value, "required")),
                        "description": text_of(_get_ci(value, "description")),
                        "schema": _param_schema(value),
                        "value": _param_value(value, _param_schema(value)),
                        "enabled": True,
                    }
                )
            elif isinstance(value, (str, int, float, bool)):
                entries.append({"name": str(key), "in": location, "value": value, "enabled": True})
        return entries
    for item in as_list(section):
        if isinstance(item, Mapping):
            name = text_of(_get_ci(item, "name", "key"))
            if not name:
                continue
            entries.append(
                {
                    "name": name,
                    "in": location,
                    "required": bool(_get_ci(item, "required")),
                    "description": text_of(_get_ci(item, "description")),
                    "schema": _param_schema(item),
                    "value": _param_value(item, _param_schema(item)),
                    "enabled": not bool(_get_ci(item, "disabled")),
                }
            )
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            entries.append({"name": text_of(item[0]), "in": location, "value": item[1], "enabled": True})
    return entries


def _media_type(body: Mapping[str, Any]) -> str:
    for key in _MEDIA_HINTS:
        value = _get_ci(body, key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    schema = _get_ci(body, "jsonSchema", "schema")
    if isinstance(schema, Mapping):
        return "application/json"
    return ""


def _body_example(body: Mapping[str, Any]) -> Any:
    example = _get_ci(body, "example", "examples", "raw", "body")
    if isinstance(example, str):
        text = example.strip()
        if text.startswith(("{", "[")):
            try:
                return json.loads(text)
            except ValueError:
                return text
        return text
    if example is not None:
        return example
    return None


class _Walker:
    def __init__(self, options: Any, warnings: WarningCollector) -> None:
        self.options = options
        self.warnings = warnings
        self.endpoints: List[EndpointSpec] = []
        self.schemas: List[Any] = []

    # -- 遍历
    def walk(self, node: Any, folder: str) -> None:
        if isinstance(node, (list, tuple)):
            for item in node:
                self.walk(item, folder)
            return
        if not isinstance(node, Mapping):
            return
        if _is_api(node):
            self.build(node, folder, _node_name(node))
            return
        wrappers = [(key, value) for key, value in node.items() if isinstance(value, Mapping) and _is_api(value)]
        if wrappers:
            name = _node_name(node)
            for _, child in wrappers:
                self.build(child, folder, name or _node_name(child))
            return
        if _api_like(node):
            self.warnings.add("跳过无法解析的接口节点: %s" % (_node_name(node) or "?"))
        name = _node_name(node)
        child_folder = join_folder(folder, name) if name else folder
        for key, value in node.items():
            if isinstance(value, (list, tuple, Mapping)):
                self.walk(value, child_folder)

    # -- 构造
    def build(self, node: Mapping[str, Any], folder: str, name: str) -> None:
        method = _normalize_method(_get_ci(node, "method"))
        path = text_of(_get_ci(node, "path", "url")).strip()
        if not method:
            self.warnings.add("跳过不支持的 method: %s %s" % (text_of(_get_ci(node, "method")), path))
            return
        if not path.startswith("/"):
            path = "/" + path
        entries = _param_entries(_get_ci(node, "parameters", "params"))
        request = RequestSpec(method=method, path=path, base_url=None)
        params: List[ParamSpec] = []
        path_values: Dict[str, str] = {}
        for entry in entries:
            location = text_of(entry.get("in") or "query").lower()
            if location == "formdata":
                location = "formdata"
            param_name = text_of(entry.get("name"))
            if not param_name:
                continue
            schema = as_mapping(entry.get("schema"))
            secret = is_secret_key(param_name)
            value = entry.get("value")
            if value is None:
                value = sample_by_type(schema.get("type"), schema.get("format"))
            if secret:
                value = ""
                self.warnings.add("敏感参数已留空: %s" % param_name)
            params.append(
                ParamSpec(
                    name=param_name,
                    location="body" if location == "formdata" else location,
                    required=bool(entry.get("required")) or location == "path",
                    type=text_of(schema.get("type")) or "string",
                    example=None if secret else value,
                    default=schema.get("default"),
                    description=text_of(entry.get("description")),
                    enum_values=list(schema.get("enum") or []),
                )
            )
            if location == "path":
                path_values[param_name] = value_to_text(value) or "1"
            elif location == "query":
                request.query.append(KeyValue(name=param_name, value=value_to_text(value), enabled=bool(entry.get("required"))))
            elif location == "header":
                request.headers.append(KeyValue(name=param_name, value=value_to_text(value), enabled=bool(entry.get("required"))))
            elif location == "cookie":
                request.cookies.append(KeyValue(name=param_name, value=value_to_text(value), enabled=bool(entry.get("required"))))
        for name_key, value_text in path_values.items():
            path = path.replace("{%s}" % name_key, value_text).replace(":%s" % name_key, value_text)
        request.path = path

        body = _get_ci(node, "requestBody", "body")
        body_node = as_mapping(body) if isinstance(body, Mapping) else {}
        form_entries = [item for item in entries if text_of(item.get("in")).lower() == "formdata"]
        if body_node or form_entries:
            media = _media_type(body_node) if body_node else ""
            if form_entries and not media:
                media = "multipart/form-data"
            body_type = body_type_for_media(media) if media else "json"
            if form_entries and body_type not in ("form", "multipart"):
                body_type = "multipart"
            request.body_type = body_type
            example = _body_example(body_node) if body_node else None
            if example is None:
                if form_entries:
                    example = {text_of(item.get("name")): item.get("value") for item in form_entries if text_of(item.get("name"))}
                else:
                    schema = as_mapping(_get_ci(body_node, "jsonSchema", "schema", "parameters"))
                    example = self._example_from_properties(schema)
            request.body = example
            header = content_type_for(body_type, media)
            if header:
                request.headers.append(KeyValue(name="Content-Type", value=header, enabled=True))

        endpoint = EndpointSpec(
            name=name or "%s %s" % (method, path),
            folder=folder,
            method=method,
            path=path,
            summary=name,
            description=text_of(_get_ci(node, "description", "remark", "notes")),
            tags=[text_of(tag) for tag in as_list(opt(self.options, "tags", [])) if text_of(tag)],
            params=params,
            request=request,
            source="apifox",
            source_ref="",
            operation_id=text_of(_get_ci(node, "id", "apiId")),
        )
        apply_default_headers(endpoint.request, self.options)
        self.endpoints.append(endpoint)
        self.schemas.append(self._response_schema(node))

    def _example_from_properties(self, schema: Mapping[str, Any]) -> Any:
        """按 properties/required 生成朴素示例（Apifox 的 jsonSchema 与 JSON Schema 一致）。"""
        if not isinstance(schema, Mapping):
            return {}
        properties = _get_ci(schema, "properties")
        if not isinstance(properties, Mapping):
            return {}
        required = [text_of(item) for item in as_list(_get_ci(schema, "required"))]
        result: Dict[str, Any] = {}
        for key, raw in properties.items():
            prop = as_mapping(raw)
            name = text_of(key)
            if prop.get("readOnly") is True:
                continue
            if is_secret_key(name):
                result[name] = ""
                self.warnings.add("敏感字段已留空: %s" % name)
                continue
            example = _get_ci(prop, "example", "default")
            if example is None:
                enum = _get_ci(prop, "enum")
                if isinstance(enum, (list, tuple)) and enum:
                    example = enum[0]
            if example is None:
                kind = text_of(_get_ci(prop, "type"))
                if kind == "object":
                    example = self._example_from_properties(prop)
                elif kind == "array":
                    example = []
                else:
                    example = sample_by_type(kind, _get_ci(prop, "format"))
            result[name] = example
        for name in required:
            if name and name not in result:
                result[name] = ""
        return result

    def _response_schema(self, node: Mapping[str, Any]) -> Any:
        responses = _get_ci(node, "responses", "response")
        candidates: List[Tuple[int, Mapping[str, Any]]] = []
        if isinstance(responses, Mapping):
            for key, value in responses.items():
                label = text_of(key)
                if label.startswith("2") and isinstance(value, Mapping):
                    candidates.append((int(label) if label.isdigit() else 299, value))
        else:
            for item in as_list(responses):
                if not isinstance(item, Mapping):
                    continue
                label = text_of(_get_ci(item, "code", "status", "statusCode"))
                if label.startswith("2"):
                    candidates.append((int(label) if label.isdigit() else 299, item))
        candidates.sort(key=lambda pair: pair[0])
        for _, value in candidates:
            schema = _get_ci(value, "jsonSchema", "schema")
            if isinstance(schema, Mapping):
                return dict(schema)
            body = _get_ci(value, "body", "example", "raw")
            if isinstance(body, str) and body.strip().startswith("{"):
                try:
                    return {"type": "object", "example": json.loads(body)}
                except ValueError:
                    continue
        return None


def parse_apifox(document: Any, source_ref: str = "", options: Any = None) -> ImportResult:
    """解析 Apifox 导出（OpenAPI 或 Apifox 自有 JSON）。"""
    if isinstance(document, Mapping) and ("openapi" in document or "swagger" in document):
        result = parse_openapi(document, source_ref, options)
        result.source = "apifox"
        result.stats.source = "apifox"
        for endpoint in result.endpoints:
            endpoint.source = "apifox"
        for case in result.cases:
            case.source = "apifox"
        return result

    warnings = WarningCollector()
    if not isinstance(document, Mapping):
        raise ValueError("无法识别的 Apifox 导出格式")
    result = ImportResult(source="apifox")
    result.stats = ImportStats(source="apifox", source_ref=text_of(source_ref))
    info = as_mapping(_get_ci(document, "info", "project"))
    result.title = text_of(_get_ci(info, "name", "title"))
    result.version = text_of(_get_ci(info, "version"))

    recognized = False
    for key in document.keys():
        if str(key).lower() in _KNOWN_TOP_KEYS:
            recognized = True
        else:
            warnings.add("未识别的顶层节点: %s" % key)

    walker = _Walker(options, warnings)
    walker.walk(document, join_folder(opt(options, "folder_prefix", ""), ""))

    if not walker.endpoints and not recognized:
        raise ValueError("无法识别的 Apifox 导出格式")

    for endpoint in walker.endpoints:
        endpoint.source_ref = text_of(source_ref)
    cases = generate_cases(walker.endpoints, options, walker.schemas)
    result.endpoints = walker.endpoints
    result.cases = cases
    result.stats.created = len(walker.endpoints)
    result.stats.cases_created = len(cases)
    result.stats.warnings = warnings.items
    return result
