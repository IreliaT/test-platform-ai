"""Postman Collection v2.1（兼容 v2.0）导入器。

实现要点
--------
* 递归遍历 ``item[]``：带 ``item`` 的节点是目录，目录名用 ``/`` 连接成 ``folder``；
* 读取 ``request.method`` / ``url``（``raw`` / ``host`` / ``path`` / ``query`` / ``variable``）、
  ``header``、``body``（``raw`` / ``urlencoded`` / ``formdata`` / ``file`` / ``graphql``）、``auth``；
* 集合级与目录级的 ``header`` / ``auth`` 会被子节点继承，子节点同名项覆盖父级；
* ``{{var}}`` 占位符原样保留，集合级 ``variable[]`` 写入 ``stats.warnings`` 的
  ``detected_variable: <name>`` 行，并作为生成用例的 ``variables``（敏感名只写入掩码）；
* 挖掘 Postman 测试脚本：``pm.response.to.have.status(<n>)`` → ``status_code eq <n>``，
  ``pm.expect(jsonData.a.b).to.eql(<字面量>)`` / ``.to.equal(...)`` / ``.to.include(...)`` →
  ``json_path`` 断言；无法识别的脚本写 ``unsupported_script: <首行>`` 警告，永不失败；
* ``response[]`` 保存的示例响应（``code``）用来补一条 ``status_code`` 断言。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlsplit

from ..models import (
    AssertionSpec,
    AuthSpec,
    EndpointSpec,
    ImportResult,
    ImportStats,
    KeyValue,
    ParamSpec,
    RequestSpec,
)
from ..redact import is_secret_key, mask
from .openapi import (
    WarningCollector,
    apply_default_headers,
    as_list,
    as_mapping,
    build_case,
    generate_cases,
    join_folder,
    opt,
    text_of,
)

__all__ = ["parse_postman", "mine_assertions"]

_STATUS_RE = re.compile(r"pm\.response\.to\.have\.status\(\s*(\d{3})\s*\)")
_EXPECT_RE = re.compile(
    r"pm\.expect\(\s*(.+?)\s*\)\s*\.to\s*\.\s*(?:deep\s*\.\s*)?(eql|equal|include|contains)\(\s*(.+?)\s*\)\s*;?\s*$"
)
_JSON_ROOT_RE = re.compile(r"^(?:pm\.response\.json\(\)|jsonData|responseJson|jsonBody)")
_ASSERTION_LIKE_RE = re.compile(r"pm\.expect\(|pm\.response\.to|tests\[")
_SIMPLE_TOKEN_RE = re.compile(r"\.([A-Za-z_$][\w$]*)")
_INDEX_TOKEN_RE = re.compile(r"\[\s*(?:'([^']*)'|\"([^\"]*)\"|(\d+))\s*\]")


def _iter_scripts(events: Any) -> List[List[str]]:
    """从 ``event[]`` 中取出测试脚本行。"""
    scripts: List[List[str]] = []
    for event in as_list(events):
        if not isinstance(event, Mapping):
            continue
        listen = text_of(event.get("listen")).lower()
        if listen and listen not in ("test", "prerequest"):
            continue
        script = event.get("script")
        if isinstance(script, Mapping):
            exec_lines = script.get("exec")
            if isinstance(exec_lines, str):
                scripts.append([exec_lines])
            elif isinstance(exec_lines, (list, tuple)):
                scripts.append([text_of(line) for line in exec_lines])
        elif isinstance(script, str):
            scripts.append(script.splitlines())
    return scripts


def _json_path_of(expression: str) -> Optional[str]:
    """把 ``jsonData.a.b`` / ``pm.response.json().a['b']`` 转成 JSONPath。"""
    expr = text_of(expression).strip()
    match = _JSON_ROOT_RE.match(expr)
    if not match:
        return None
    rest = expr[match.end() :]
    tokens: List[str] = []
    pos = 0
    while pos < len(rest):
        chunk = rest[pos:]
        simple = _SIMPLE_TOKEN_RE.match(chunk)
        if simple:
            tokens.append(simple.group(1))
            pos += simple.end()
            continue
        index = _INDEX_TOKEN_RE.match(chunk)
        if index:
            token = index.group(1)
            if token is None:
                token = index.group(2)
            if token is None:
                token = index.group(3)
            tokens.append(text_of(token))
            pos += index.end()
            continue
        return None
    if not tokens:
        return None
    path = "$"
    for token in tokens:
        if re.match(r"^[A-Za-z_$][\w$]*$", token):
            path += ".%s" % token
        else:
            path += "['%s']" % token.replace("'", "\\'")
    return path


def _parse_literal(text: str) -> Tuple[bool, Any]:
    """解析脚本里的字面量；不支持的写法返回 (False, None)。"""
    value = text_of(text).strip().rstrip(";").strip()
    if not value:
        return False, None
    if value in ("true", "True"):
        return True, True
    if value in ("false", "False"):
        return True, False
    if value in ("null", "None", "undefined"):
        return True, None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        inner = value[1:-1]
        inner = inner.replace("\\'", "'").replace('\\"', '"').replace("\\n", "\n")
        return True, inner
    if re.match(r"^-?\d+$", value):
        return True, int(value)
    if re.match(r"^-?\d+\.\d+$", value):
        return True, float(value)
    return False, None


def mine_assertions(events: Any, warnings: Optional[WarningCollector] = None) -> List[AssertionSpec]:
    """挖掘 Postman 测试脚本中的断言（无法识别时只记警告，绝不抛异常）。"""
    collector = warnings if warnings is not None else WarningCollector()
    assertions: List[AssertionSpec] = []
    for lines in _iter_scripts(events):
        mined = 0
        unsupported = ""
        for line in lines:
            text = text_of(line).strip()
            if not text or text.startswith("//"):
                continue
            before = mined
            for match in _STATUS_RE.finditer(text):
                assertions.append(
                    AssertionSpec(
                        type="status_code",
                        operator="eq",
                        expected=int(match.group(1)),
                        message="Postman 脚本断言状态码",
                    )
                )
                mined += 1
            for match in _EXPECT_RE.finditer(text):
                path = _json_path_of(match.group(1))
                ok, literal = _parse_literal(match.group(3))
                if path is None or not ok:
                    continue
                operator = "eq" if match.group(2) in ("eql", "equal") else "contains"
                assertions.append(
                    AssertionSpec(type="json_path", operator=operator, expected=literal, path=path)
                )
                mined += 1
            if mined == before and not unsupported and _ASSERTION_LIKE_RE.search(text):
                unsupported = text
        if unsupported:
            collector.add("unsupported_script: %s" % unsupported)
    return assertions


def _parse_raw_url(raw: str) -> Tuple[Optional[str], str, List[Tuple[str, str]]]:
    """拆分 raw URL：返回 (base_url 或 None, path, query 键值对)。"""
    text = text_of(raw).strip()
    if not text:
        return None, "/", []
    parts = urlsplit(text)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if parts.scheme and parts.netloc:
        base = "%s://%s" % (parts.scheme, parts.netloc)
        return base, parts.path or "/", query
    if parts.netloc:
        return "//%s" % parts.netloc, parts.path or "/", query
    if parts.scheme and "." in parts.scheme and not parts.scheme.startswith("{{"):
        # 形如 api.example.com:8080/x —— urlsplit 会把主机名误判成 scheme
        head, sep, rest = parts.path.partition("/")
        base = "http://%s" % parts.scheme
        if head.isdigit():
            base += ":%s" % head
            return base, ("/" + rest) if sep else "/", query
        return base, "/" + parts.path if parts.path else "/", query
    bare = text.split("?", 1)[0].split("#", 1)[0].strip()
    if not bare:
        return None, "/", query
    if bare.startswith(("/", "{{", "${", ":")):
        return None, bare, query
    first, sep, rest = bare.partition("/")
    if "." in first or ":" in first:
        return "http://%s" % first, ("/" + rest) if sep else "/", query
    return None, ("/" + bare) if not bare.startswith("/") else bare, query


def _url_from_parts(url: Mapping[str, Any]) -> str:
    """由 host/path/query 字段拼出 raw URL。"""
    host = url.get("host")
    if isinstance(host, (list, tuple)):
        host_text = ".".join(text_of(item) for item in host)
    else:
        host_text = text_of(host)
    port = text_of(url.get("port"))
    if port:
        host_text = "%s:%s" % (host_text, port)
    protocol = text_of(url.get("protocol"))
    path = url.get("path")
    if isinstance(path, (list, tuple)):
        path_text = "/".join(text_of(item) for item in path)
    else:
        path_text = text_of(path).lstrip("/")
    query: List[str] = []
    for item in as_list(url.get("query")):
        if isinstance(item, Mapping):
            key = text_of(item.get("key"))
            if not key or item.get("disabled"):
                continue
            value = text_of(item.get("value"))
            query.append("%s=%s" % (key, value))
    prefix = ("%s://" % protocol) if protocol else ""
    result = "%s%s/%s" % (prefix, host_text, path_text)
    if query:
        result += "?" + "&".join(query)
    return result


def _query_entries(url: Mapping[str, Any], raw_query: Sequence[Tuple[str, str]]) -> List[KeyValue]:
    entries: List[KeyValue] = []
    seen = set()
    for item in as_list(url.get("query")):
        if not isinstance(item, Mapping):
            continue
        key = text_of(item.get("key"))
        if not key or key in seen:
            continue
        seen.add(key)
        entries.append(
            KeyValue(
                name=key,
                value=text_of(item.get("value")),
                enabled=not bool(item.get("disabled")),
                description=text_of(item.get("description")),
            )
        )
    for key, value in raw_query:
        if key in seen:
            continue
        seen.add(key)
        entries.append(KeyValue(name=key, value=value, enabled=True))
    return entries


def _header_entries(raw_headers: Any) -> List[Tuple[str, KeyValue]]:
    result: List[Tuple[str, KeyValue]] = []
    for item in as_list(raw_headers):
        if isinstance(item, str):
            name, _, value = item.partition(":")
            name = name.strip()
            if name:
                result.append((name.lower(), KeyValue(name=name, value=value.strip(), enabled=True)))
            continue
        if not isinstance(item, Mapping):
            continue
        name = text_of(item.get("key") or item.get("name")).strip()
        if not name:
            continue
        result.append(
            (
                name.lower(),
                KeyValue(
                    name=name,
                    value=text_of(item.get("value")),
                    enabled=not bool(item.get("disabled")),
                    description=text_of(item.get("description")),
                ),
            )
        )
    return result


def _merge_headers(parent: Sequence[KeyValue], child: Any) -> List[KeyValue]:
    merged: Dict[str, KeyValue] = {item.name.lower(): item for item in parent}
    for key, item in _header_entries(child):
        current = merged.get(key)
        if current is not None:
            item.enabled = item.enabled and current.enabled
        merged[key] = item
    return list(merged.values())


def _auth_value(entries: Any, key: str) -> str:
    for item in as_list(entries):
        if isinstance(item, Mapping) and text_of(item.get("key")).lower() == key.lower():
            return text_of(item.get("value"))
    return ""


def _auth_spec(raw_auth: Any, inherited: AuthSpec, warnings: WarningCollector) -> AuthSpec:
    if not isinstance(raw_auth, Mapping):
        return inherited
    kind = text_of(raw_auth.get("type")).strip().lower()
    if not kind:
        return inherited
    if kind == "noauth":
        return AuthSpec(type="none")
    if kind == "bearer":
        return AuthSpec(type="bearer", token=_auth_value(raw_auth.get("bearer"), "token"))
    if kind == "basic":
        return AuthSpec(
            type="basic",
            username=_auth_value(raw_auth.get("basic"), "username"),
            password=_auth_value(raw_auth.get("basic"), "password"),
        )
    if kind == "apikey":
        entries = raw_auth.get("apikey")
        location = _auth_value(entries, "in") or "header"
        return AuthSpec(
            type="api_key",
            key=_auth_value(entries, "key"),
            value=_auth_value(entries, "value"),
            location="query" if location.lower() == "query" else "header",
        )
    if kind == "oauth2":
        warnings.add("oauth2 认证按 bearer 处理")
        return AuthSpec(type="bearer", token=_auth_value(raw_auth.get("oauth2"), "accessToken"))
    warnings.add("不支持的认证类型: %s（已忽略）" % kind)
    return inherited


def _body_of(request: Mapping[str, Any], warnings: WarningCollector) -> Tuple[str, Any]:
    body = request.get("body")
    if not isinstance(body, Mapping):
        return "none", None
    mode = text_of(body.get("mode")).strip().lower()
    if mode == "raw":
        raw = text_of(body.get("raw"))
        language = text_of(as_mapping(as_mapping(body.get("options")).get("raw")).get("language")).lower()
        if language == "json" or (raw.strip().startswith(("{", "[")) and raw.strip().endswith(("}", "]"))):
            try:
                return "json", json.loads(raw)
            except ValueError:
                if language == "json":
                    warnings.add("raw body 声明为 json 但无法解析，已按原文保留")
                    return "raw", raw
        return "raw", raw
    if mode in ("urlencoded", "formdata"):
        values: Dict[str, Any] = {}
        for item in as_list(body.get(mode)):
            if not isinstance(item, Mapping) or item.get("disabled"):
                continue
            key = text_of(item.get("key"))
            if not key:
                continue
            if text_of(item.get("type")).lower() == "file":
                values[key] = text_of(item.get("src"))
                warnings.add("formdata 文件字段按路径字符串保留: %s" % key)
            else:
                values[key] = text_of(item.get("value"))
        return ("form" if mode == "urlencoded" else "multipart"), values
    if mode == "file":
        source = text_of(as_mapping(body.get("file")).get("src"))
        warnings.add("file 请求体按 raw 文本导入（不支持本地文件上传）")
        return "raw", source
    if mode == "graphql":
        payload = as_mapping(body.get("graphql"))
        warnings.add("graphql 请求体按 json 导入")
        return "json", {"query": text_of(payload.get("query")), "variables": payload.get("variables") or {}}
    if mode:
        warnings.add("不支持的 body 模式: %s（已忽略）" % mode)
    return "none", None


class _Walker:
    def __init__(self, options: Any, warnings: WarningCollector) -> None:
        self.options = options
        self.warnings = warnings
        self.endpoints: List[EndpointSpec] = []
        self.assertions: List[List[AssertionSpec]] = []
        self.skipped = 0

    # -- 遍历
    def walk(self, items: Any, folder: str, headers: Sequence[KeyValue], auth: AuthSpec) -> None:
        for item in as_list(items):
            if not isinstance(item, Mapping):
                continue
            name = text_of(item.get("name"))
            if isinstance(item.get("item"), (list, tuple)):
                self.walk(
                    item.get("item"),
                    join_folder(folder, name),
                    _merge_headers(headers, item.get("header")),
                    _auth_spec(item.get("auth"), auth, self.warnings),
                )
                continue
            request = item.get("request")
            if isinstance(request, str):
                request = {"method": "GET", "url": request}
            if not isinstance(request, Mapping):
                self.skipped += 1
                if name:
                    self.warnings.add("跳过无法识别的节点: %s" % name)
                continue
            self._build_item(item, request, folder, headers, auth)

    # -- 单个请求
    def _build_item(
        self,
        item: Mapping[str, Any],
        request: Mapping[str, Any],
        folder: str,
        inherited_headers: Sequence[KeyValue],
        inherited_auth: AuthSpec,
    ) -> None:
        method = text_of(request.get("method") or "GET").strip().upper() or "GET"
        raw_url = request.get("url")
        url_map: Dict[str, Any] = {}
        if isinstance(raw_url, Mapping):
            url_map = dict(raw_url)
            raw_text = text_of(url_map.get("raw")) or _url_from_parts(url_map)
        else:
            raw_text = text_of(raw_url)
        base_url, path, raw_query = _parse_raw_url(raw_text)
        query_entries = _query_entries(url_map, raw_query)

        # Postman 的 :id / {{id}} 路径变量
        path_params: List[ParamSpec] = []
        for variable in as_list(url_map.get("variable")):
            if not isinstance(variable, Mapping):
                continue
            key = text_of(variable.get("key"))
            if not key:
                continue
            value = text_of(variable.get("value"))
            path_params.append(ParamSpec(name=key, location="path", required=True, type="string", example=value))
            path = path.replace(":%s" % key, value or key)
            path = path.replace("{{%s}}" % key, value or key)

        headers = _merge_headers(inherited_headers, request.get("header"))
        auth = _auth_spec(request.get("auth"), inherited_auth, self.warnings)
        body_type, body = _body_of(request, self.warnings)

        name = text_of(item.get("name")) or "%s %s" % (method, path)
        spec = RequestSpec(method=method, path=path, base_url=base_url, query=query_entries,
                           headers=list(headers), body_type=body_type, body=body, auth=auth)
        params: List[ParamSpec] = list(path_params)
        for entry in query_entries:
            params.append(
                ParamSpec(
                    name=entry.name,
                    location="query",
                    required=False,
                    type="string",
                    example=entry.value,
                    description=entry.description,
                )
            )
        for entry in headers:
            params.append(
                ParamSpec(
                    name=entry.name,
                    location="header",
                    required=False,
                    type="string",
                    example=entry.value,
                    description=entry.description,
                )
            )
        endpoint = EndpointSpec(
            name=name,
            folder=folder,
            method=method,
            path=path,
            summary=name,
            description=_description_of(item.get("description") or request.get("description")),
            tags=[text_of(tag) for tag in as_list(opt(self.options, "tags", [])) if text_of(tag)],
            params=params,
            request=spec,
            source="postman",
            operation_id=text_of(item.get("id")),
        )
        apply_default_headers(endpoint.request, self.options)
        self.endpoints.append(endpoint)
        self.assertions.append(self._mine(item))

    def _mine(self, item: Mapping[str, Any]) -> List[AssertionSpec]:
        """脚本断言优先；脚本没给状态码时，用保存的示例响应补一条。"""
        mined = mine_assertions(item.get("event"), self.warnings)
        if any(assertion.type == "status_code" for assertion in mined):
            return mined
        for response in as_list(item.get("response")):
            if not isinstance(response, Mapping):
                continue
            code = response.get("code")
            if isinstance(code, int):
                return [
                    AssertionSpec(
                        type="status_code",
                        operator="eq",
                        expected=code,
                        message="取自 Postman 保存的示例响应",
                    )
                ] + mined
        return mined


def _description_of(value: Any) -> str:
    if isinstance(value, Mapping):
        return text_of(value.get("content"))
    return text_of(value)


def parse_postman(document: Any, source_ref: str = "", options: Any = None) -> ImportResult:
    """解析 Postman Collection v2.1 / v2.0。"""
    if not isinstance(document, Mapping):
        raise ValueError("Postman 集合必须是 JSON 对象")
    warnings = WarningCollector()
    info = as_mapping(document.get("info"))
    ref = text_of(source_ref) or text_of(info.get("name"))
    result = ImportResult(source="postman", title=text_of(info.get("name")), version=text_of(info.get("version")))
    result.stats = ImportStats(source="postman", source_ref=ref)

    variables: Dict[str, Any] = {}
    for variable in as_list(document.get("variable")):
        if isinstance(variable, Mapping):
            key = text_of(variable.get("key") or variable.get("id"))
            value = variable.get("value")
        elif isinstance(variable, (list, tuple)) and len(variable) == 2:
            key, value = text_of(variable[0]), variable[1]
        else:
            continue
        if not key:
            continue
        warnings.add("detected_variable: %s" % key)
        variables[key] = mask(value) if is_secret_key(key) else value

    walker = _Walker(options, warnings)
    walker.walk(
        document.get("item"),
        join_folder(opt(options, "folder_prefix", ""), ""),
        _merge_headers([], document.get("header")),
        _auth_spec(document.get("auth"), AuthSpec(), warnings),
    )

    for endpoint in walker.endpoints:
        endpoint.source_ref = ref
    cases = generate_cases(walker.endpoints, options, None, variables, walker.assertions)
    result.endpoints = walker.endpoints
    result.cases = cases
    result.stats.created = len(walker.endpoints)
    result.stats.skipped = walker.skipped
    result.stats.cases_created = len(cases)
    result.stats.warnings = warnings.items
    return result
