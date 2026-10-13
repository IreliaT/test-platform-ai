"""curl 命令导入器：粘贴一条 curl 命令 → 一个接口 + 一条冒烟用例。

支持的行内语法
--------------
* 换行续行（``\\`` + 换行）、单/双引号、``$VAR`` 原样保留
  （``shlex`` 只做分词，不做变量展开，因此 ``$`` 不会被吃掉）；
* ``-X/--request``、``-H/--header``、``-d/--data/--data-raw/--data-binary/--data-urlencode``、
  ``-F/--form``、``-u/--user``、``-b/--cookie``、``-A/--user-agent``、``-e/--referer``、
  ``-m/--max-time``、``--connect-timeout``、``--url``；
* 无值开关：``--compressed``、``-k/--insecure``、``-L/--location``、``-G/--get``、
  ``-s``、``-i``、``-v``、``-I/--head``。

推断规则
--------
* ``-F`` → ``multipart``；``--data-urlencode`` → ``form``；``-d`` 能解析成 JSON
  或显式声明 ``Content-Type: application/json`` → ``json``；
  纯 ``k=v&k2=v2`` 形式的 ``-d``（curl 默认会补 urlencoded）→ ``form``；其余 → ``raw``；
* ``-k`` → ``request.verify_tls = False``；``-L`` → 跟随重定向；``-G`` → 数据拼到 query；
* URL 拆成 ``base_url``（``scheme://host[:port]``）+ ``path`` + ``query`` 条目。

安全：``source_ref`` 取脱敏后的 URL（敏感 query 参数替换为掩码），不会把令牌写进库里。
"""

from __future__ import annotations

import json
import re
import shlex
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlsplit

from ..models import (
    AuthSpec,
    EndpointSpec,
    ImportResult,
    ImportStats,
    KeyValue,
    ParamSpec,
    RequestSpec,
)
from ..redact import is_secret_key, redact_url
from .openapi import (
    WarningCollector,
    apply_default_headers,
    as_list,
    generate_cases,
    join_folder,
    opt,
    text_of,
    value_to_text,
)

__all__ = ["parse_curl"]

_VALUE_FLAGS = {
    "-X": "method",
    "--request": "method",
    "-H": "header",
    "--header": "header",
    "-d": "data",
    "--data": "data",
    "--data-raw": "data",
    "--data-ascii": "data",
    "--data-binary": "data",
    "--data-urlencode": "urlencode",
    "-F": "form",
    "--form": "form",
    "--form-string": "form",
    "-u": "user",
    "--user": "user",
    "-b": "cookie",
    "--cookie": "cookie",
    "-A": "user_agent",
    "--user-agent": "user_agent",
    "-e": "referer",
    "--referer": "referer",
    "-m": "max_time",
    "--max-time": "max_time",
    "--connect-timeout": "connect_timeout",
    "-o": "output",
    "--output": "output",
    "--url": "url",
    "-x": "proxy",
    "--proxy": "proxy",
    "-T": "upload",
    "--upload-file": "upload",
}

_SWITCH_FLAGS = {
    "--compressed": "compressed",
    "-k": "insecure",
    "--insecure": "insecure",
    "-L": "location",
    "--location": "location",
    "-G": "get",
    "--get": "get",
    "-I": "head",
    "--head": "head",
    "-s": "silent",
    "--silent": "silent",
    "-i": "include",
    "--include": "include",
    "-v": "verbose",
    "--verbose": "verbose",
    "-f": "fail",
    "--fail": "fail",
    "-S": "show_error",
    "--show-error": "show_error",
    "--no-buffer": "no_buffer",
}

_JSON_TYPES = ("application/json", "text/json")
_FORM_TYPE = "application/x-www-form-urlencoded"
_FORM_KEY_RE = re.compile(r"^[A-Za-z0-9_.\-\[\]%]+$")

_CONTINUATION_RE = re.compile(r"\\\s*\r?\n\s*")
_PROMPT_RE = re.compile(r"^\s*[\$>#]\s*")


def _prepare_text(command: str) -> str:
    text = text_of(command)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTINUATION_RE.sub(" ", text)
    text = _PROMPT_RE.sub("", text.strip())
    return text.strip()


def _tokenize(command: str) -> List[str]:
    """用 ``shlex``（posix 模式）分词；不做变量展开，``$VAR`` 原样保留。"""
    try:
        return shlex.split(command, posix=True)
    except ValueError as exc:
        raise ValueError("curl 命令无法分词（引号可能不匹配）: %s" % exc)


def _split_data_pairs(parts: Sequence[str]) -> Optional[Dict[str, str]]:
    """判断 ``k=v&k2=v2`` 形式的数据；不是则返回 None。"""
    result: Dict[str, str] = {}
    if not parts:
        return None
    for part in parts:
        for chunk in part.split("&"):
            if not chunk:
                continue
            key, sep, value = chunk.partition("=")
            if not sep or not _FORM_KEY_RE.match(key):
                return None
            result[key] = value
    return result or None


def _urlencode_pairs(parts: Sequence[str], warnings: WarningCollector) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for part in parts:
        text = text_of(part)
        if text.startswith("@"):
            warnings.add("不支持 --data-urlencode 读取文件: %s" % text)
            continue
        key, sep, value = text.partition("=")
        if not sep:
            key, sep, value = text.partition("@")
            if sep:
                warnings.add("不支持 --data-urlencode 读取文件: %s" % text)
                continue
            result[text] = ""
            continue
        result[key] = value
    return result


def _form_pairs(parts: Sequence[str], warnings: WarningCollector) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for part in parts:
        text = text_of(part)
        key, sep, value = text.partition("=")
        if not sep:
            result[text] = ""
            continue
        if value.startswith("@"):
            warnings.add("不支持表单字段上传文件: %s" % text)
        result[key] = value
    return result


def _content_type_header(headers: Sequence[KeyValue]) -> str:
    for item in headers:
        if item.name.lower() == "content-type":
            return item.value.split(";")[0].strip().lower()
    return ""


def _split_url(raw: str, warnings: WarningCollector) -> Tuple[Optional[str], str, List[Tuple[str, str]]]:
    """拆分 URL：返回 (base_url 或 None, path, query 键值对)。"""
    text = text_of(raw).strip()
    if not text:
        return None, "/", []
    if text.startswith(("{{", "${")):
        return None, text, []
    parts = urlsplit(text)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if "://" in text and parts.scheme and parts.netloc:
        return "%s://%s" % (parts.scheme, parts.netloc), parts.path or "/", query
    if text.startswith("//") and parts.netloc:
        return "//%s" % parts.netloc, parts.path or "/", query
    bare = text.split("?", 1)[0].split("#", 1)[0]
    if bare.startswith(("{{", "${", "/")):
        return None, bare or "/", query
    first, sep, rest = bare.partition("/")
    if "." in first or ":" in first:
        warnings.add("URL 缺少协议，已按 http:// 处理")
        return "http://%s" % first, ("/" + rest) if sep else "/", query
    return None, bare if bare.startswith("/") else "/" + bare, query


def parse_curl(command: str, options: Any = None) -> ImportResult:
    """解析一条 curl 命令，产出 1 个接口与 1 条冒烟用例。"""
    warnings = WarningCollector()
    tokens = _tokenize(_prepare_text(command))
    if tokens and tokens[0].endswith("curl"):
        tokens = tokens[1:]
    if not tokens:
        raise ValueError("curl 命令为空，无法解析")

    method = ""
    auth = AuthSpec()
    headers: List[KeyValue] = []
    data_parts: List[str] = []
    urlencode_parts: List[str] = []
    form_parts: List[str] = []
    cookies: List[KeyValue] = []
    user = ""
    url = ""
    timeout_ms: Optional[int] = None
    verify_tls = True
    follow_redirects = False
    use_get = False
    compressed = False

    index = 0
    while index < len(tokens):
        token = tokens[index]
        flag = token
        value: Optional[str] = None
        if token.startswith("--") and "=" in token:
            flag, _, value = token.partition("=")
        elif token.startswith("-") and not token.startswith("--") and len(token) > 2:
            head = token[:2]
            if head in _VALUE_FLAGS:
                flag, value = head, token[2:]
            else:
                for char in token[1:]:
                    single = "-" + char
                    if single in _SWITCH_FLAGS:
                        switch = _SWITCH_FLAGS[single]
                        if switch == "insecure":
                            verify_tls = False
                        elif switch == "location":
                            follow_redirects = True
                        elif switch == "get":
                            use_get = True
                        elif switch == "compressed":
                            compressed = True
                        elif switch == "head":
                            method = method or "HEAD"
                    else:
                        warnings.add("未识别的 curl 选项: %s" % single)
                index += 1
                continue

        if flag in _SWITCH_FLAGS:
            switch = _SWITCH_FLAGS[flag]
            if switch == "insecure":
                verify_tls = False
            elif switch == "location":
                follow_redirects = True
            elif switch == "get":
                use_get = True
            elif switch == "compressed":
                compressed = True
            elif switch == "head":
                method = method or "HEAD"
            index += 1
            continue

        kind = _VALUE_FLAGS.get(flag)
        if kind is None:
            if token.startswith("-") and token != "-":
                warnings.add("未识别的 curl 选项: %s" % token)
                index += 1
                continue
            if not url:
                url = token
            else:
                warnings.add("忽略多余的 URL 参数: %s" % token)
            index += 1
            continue

        if value is None:
            index += 1
            if index >= len(tokens):
                warnings.add("curl 选项缺少取值: %s" % flag)
                break
            value = tokens[index]
        index += 1

        if kind == "method":
            method = text_of(value).upper()
        elif kind == "header":
            name, sep, header_value = text_of(value).partition(":")
            name = name.strip()
            if not name or not sep:
                warnings.add("忽略无法解析的请求头: %s" % value)
                continue
            if not header_value.strip():
                warnings.add("忽略空值请求头: %s" % name)
                continue
            headers.append(KeyValue(name=name, value=header_value.strip(), enabled=True))
        elif kind == "data":
            data_parts.append(text_of(value))
        elif kind == "urlencode":
            urlencode_parts.append(text_of(value))
        elif kind == "form":
            form_parts.append(text_of(value))
        elif kind == "user":
            username, _, password = text_of(value).partition(":")
            user = username
            auth = AuthSpec(type="basic", username=username, password=password)
        elif kind == "cookie":
            text = text_of(value)
            if "=" not in text:
                warnings.add("不支持 cookie 文件: %s" % text)
                continue
            for chunk in text.split(";"):
                name, _, cookie_value = chunk.strip().partition("=")
                if name:
                    cookies.append(KeyValue(name=name, value=cookie_value, enabled=True))
        elif kind == "user_agent":
            headers.append(KeyValue(name="User-Agent", value=text_of(value), enabled=True))
        elif kind == "referer":
            headers.append(KeyValue(name="Referer", value=text_of(value), enabled=True))
        elif kind == "max_time":
            try:
                timeout_ms = int(float(text_of(value)) * 1000)
            except ValueError:
                warnings.add("无法解析 --max-time: %s" % value)
        elif kind == "connect_timeout":
            try:
                timeout_ms = timeout_ms or int(float(text_of(value)) * 1000)
            except ValueError:
                warnings.add("无法解析 --connect-timeout: %s" % value)
        elif kind == "url":
            url = text_of(value)
        elif kind in ("output", "proxy"):
            warnings.add("curl 选项 %s 对导入无影响" % flag)
        elif kind == "upload":
            warnings.add("不支持 --upload-file（文件上传）")

    if user and auth.type != "basic":
        username, _, password = user.partition(":")
        auth = AuthSpec(type="basic", username=username, password=password)

    if use_get:
        method = method or "GET"
        if data_parts:
            url = _append_query(url, [(part.partition("=")[0], part.partition("=")[2]) for part in data_parts])
        if urlencode_parts:
            pairs = _urlencode_pairs(urlencode_parts, warnings)
            url = _append_query(url, list(pairs.items()))
        data_parts = []
        urlencode_parts = []

    base_url, path, query_pairs = _split_url(url, warnings)
    if not path:
        path = "/"

    body_type = "none"
    body: Any = None
    if form_parts:
        body_type = "multipart"
        body = _form_pairs(form_parts, warnings)
    elif urlencode_parts:
        body_type = "form"
        body = _urlencode_pairs(urlencode_parts, warnings)
    elif data_parts:
        joined = "&".join(data_parts)
        content_type = _content_type_header(headers)
        parsed_json = None
        if joined.strip().startswith(("{", "[")):
            try:
                parsed_json = json.loads(joined)
            except ValueError:
                parsed_json = None
        if parsed_json is not None:
            body_type = "json"
            body = parsed_json
        elif content_type in _JSON_TYPES or content_type.endswith("+json"):
            warnings.add("请求体声明为 JSON 但无法解析，已按原文保留")
            body_type = "raw"
            body = joined
        elif content_type == _FORM_TYPE:
            body_type = "form"
            body = _split_data_pairs(data_parts) or {}
        elif not content_type and _split_data_pairs(data_parts) is not None:
            body_type = "form"
            body = _split_data_pairs(data_parts) or {}
        else:
            body_type = "raw"
            body = joined
    if method == "":
        method = "GET" if body_type == "none" else "POST"
    if compressed:
        headers.append(KeyValue(name="Accept-Encoding", value="gzip, deflate", enabled=True))
    if not _content_type_header(headers):
        if body_type == "form":
            headers.append(KeyValue(name="Content-Type", value=_FORM_TYPE, enabled=True))
        elif body_type == "multipart":
            headers.append(KeyValue(name="Content-Type", value="multipart/form-data", enabled=True))

    query_entries = [KeyValue(name=name, value=value, enabled=True) for name, value in query_pairs]
    params: List[ParamSpec] = []
    for name, value in query_pairs:
        params.append(
            ParamSpec(name=name, location="query", required=False, type="string",
                      example=None if is_secret_key(name) else value)
        )
    for name in re.findall(r"\{([^}/]+)\}", path):
        params.append(ParamSpec(name=name, location="path", required=True, type="string", example=None))

    request = RequestSpec(
        method=method,
        path=path,
        base_url=base_url,
        query=query_entries,
        headers=headers,
        cookies=cookies,
        body_type=body_type,
        body=body,
        auth=auth,
        timeout_ms=timeout_ms,
        follow_redirects=follow_redirects,
        verify_tls=verify_tls,
    )
    endpoint = EndpointSpec(
        name="%s %s" % (method, path),
        folder=join_folder(opt(options, "folder_prefix", ""), ""),
        method=method,
        path=path,
        summary="%s %s" % (method, path),
        description="",
        tags=[text_of(tag) for tag in as_list(opt(options, "tags", [])) if text_of(tag)],
        params=params,
        request=request,
        source="curl",
        source_ref=redact_url(url),
    )
    apply_default_headers(endpoint.request, options)

    result = ImportResult(source="curl", title="%s %s" % (method, path))
    result.stats = ImportStats(source="curl", source_ref=endpoint.source_ref)
    result.endpoints = [endpoint]
    cases = generate_cases(result.endpoints, options)
    result.cases = cases
    result.stats.created = 1
    result.stats.cases_created = len(cases)
    result.stats.warnings = warnings.items
    return result



def _append_query(url: str, pairs: Sequence[Tuple[str, str]]) -> str:
    if not pairs:
        return url
    extra = "&".join("%s=%s" % (key, value) for key, value in pairs)
    if not url:
        return "?" + extra
    return url + ("&" if "?" in url else "?") + extra
