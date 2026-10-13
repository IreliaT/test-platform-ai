"""Spring 源码扫描导入器（不依赖编译产物，纯文本 + 结构化扫描）。

目标仓库使用 springfox 2.7.0，且只有少量 Controller 暴露在 spec 里，
因此源码扫描是本平台同步接口的主要途径。本模块的能力：

* 识别 ``@RestController`` / ``@Controller`` 类与类级 ``@RequestMapping`` 前缀；
* 识别 ``@GetMapping`` / ``@PostMapping`` / ``@PutMapping`` / ``@DeleteMapping`` /
  ``@PatchMapping`` / 方法级 ``@RequestMapping``，``value`` / ``path`` 均支持，
  数组形式（``value = {"/a", "/b"}``）为每个路径生成一个接口；
* 解析 ``@PathVariable`` / ``@RequestParam`` / ``@RequestBody`` / ``@RequestHeader`` /
  ``@RequestPart`` / ``@CookieValue`` / ``@ModelAttribute``，Java 类型映射为 JSON 类型；
* 构建文件级与全局的 Java 字符串常量符号表（``static final String X = "..."``），
  把 ``@RequestMapping(API_PREFIX + "/users")`` 里的常量替换成真实值，未解析的常量记 warning；
* 解析 springfox 注解：``@ApiOperation``（summary/notes）、``@Api``（描述/标签）、
  ``@ApiParam``、``@ApiModelProperty``，以及 springdoc 的 ``@Tag`` / ``@Schema`` / ``@Parameter``；
* 当 ``@RequestBody`` 的类型能在同源码树里找到对应 DTO 时，按 DTO 字段生成请求体示例；
* 读取 ``src/main/resources/application*.{yml,yaml,properties}``：
  ``server.servlet.context-path``（兼容 Spring Boot 1.x 的 ``server.context-path``）、
  ``server.port``、``spring.application.name``，并在 warnings 里输出
  ``detected_context_path: /x`` / ``detected_server_port: N`` / ``detected_application_name: X``，
  同时用 context-path 前缀接口路径。

任何单个文件解析失败都只记 warning 并继续，绝不中断整次导入。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

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
    WarningCollector,
    apply_default_headers,
    as_list,
    generate_cases,
    join_folder,
    opt,
    sample_by_type,
    text_of,
    value_to_text,
)
from .yaml_lite import load_yaml

__all__ = ["scan_spring_project"]

SKIP_DIRS = frozenset(
    ["target", "build", "out", "dist", ".git", ".idea", ".gradle", ".mvn", "node_modules", ".settings"]
)
MAX_FILE_BYTES = 1024 * 1024
MAX_NESTING = 6

MAPPING_ANNOTATIONS = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
    "RequestMapping": "",
}

_BINDING_ANNOTATIONS = (
    "PathVariable",
    "RequestParam",
    "RequestBody",
    "RequestHeader",
    "RequestPart",
    "CookieValue",
    "ModelAttribute",
)

_PARAM_ANNOTATIONS = (
    "PathVariable",
    "RequestParam",
    "RequestBody",
    "RequestHeader",
    "RequestPart",
    "CookieValue",
    "ModelAttribute",
    "ApiParam",
    "Parameter",
)

_REQUIRED_ANNOTATIONS = ("NotNull", "NotBlank", "NotEmpty", "NonNull")

_JAVA_TYPE_JSON: Dict[str, str] = {
    "string": "string",
    "char": "string",
    "character": "string",
    "charsequence": "string",
    "object": "object",
    "int": "integer",
    "integer": "integer",
    "long": "integer",
    "short": "integer",
    "byte": "integer",
    "biginteger": "integer",
    "atomicinteger": "integer",
    "atomiclong": "integer",
    "double": "number",
    "float": "number",
    "bigdecimal": "number",
    "number": "number",
    "boolean": "boolean",
    "atomicboolean": "boolean",
    "list": "array",
    "set": "array",
    "collection": "array",
    "iterable": "array",
    "arraylist": "array",
    "linkedlist": "array",
    "map": "object",
    "hashmap": "object",
    "linkedhashmap": "object",
    "jsonobject": "object",
    "multipartfile": "string",
    "localdate": "string",
    "localdatetime": "string",
    "localdatetime": "string",
    "date": "string",
    "timestamp": "string",
    "uuid": "string",
    "void": "null",
}

_JAVA_TYPE_FORMAT: Dict[str, str] = {
    "localdate": "date",
    "localdatetime": "date-time",
    "date": "date-time",
    "uuid": "uuid",
    "bigdecimal": "double",
    "multipartfile": "binary",
}

_METHOD_KEYWORDS = frozenset(
    [
        "if",
        "for",
        "while",
        "switch",
        "catch",
        "return",
        "new",
        "synchronized",
        "do",
        "else",
        "try",
        "finally",
    ]
)


# ---------------------------------------------------------------------------
# 文本扫描工具
# ---------------------------------------------------------------------------


def _strip_comments(text: str) -> str:
    """去掉 // 与 /* */ 注释（保留换行与偏移，字符串字面量内部不处理）。"""
    out: List[str] = []
    i = 0
    length = len(text)
    while i < length:
        ch = text[i]
        if ch in "\"'":
            end = _skip_string(text, i)
            out.append(text[i:end])
            i = end
            continue
        if ch == "/" and i + 1 < length and text[i + 1] == "/":
            while i < length and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < length and text[i + 1] == "*":
            i += 2
            while i + 1 < length and not (text[i] == "*" and text[i + 1] == "/"):
                if text[i] == "\n":
                    out.append("\n")
                i += 1
            i = min(i + 2, length)
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _skip_string(text: str, start: int) -> int:
    """跳过字符串/字符字面量，返回结束后的下标。"""
    quote = text[start]
    if text.startswith(quote * 3, start):
        end = text.find(quote * 3, start + 3)
        return len(text) if end < 0 else end + 3
    i = start + 1
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == quote:
            return i + 1
        i += 1
    return len(text)


def _skip_balanced(text: str, start: int, open_ch: str = "(", close_ch: str = ")") -> int:
    """跳过配对括号，返回闭合符号之后的下标。"""
    depth = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch in "\"'":
            i = _skip_string(text, i)
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(text)


def _statement_end(text: str, start: int, end: int) -> int:
    """找到语句结束的 ``;``（忽略字符串、括号与数组初始化块）。"""
    depth = 0
    i = start
    while i < end:
        ch = text[i]
        if ch in "\"'":
            i = _skip_string(text, i)
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == ";" and depth <= 0:
            return i
        i += 1
    return end


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, max(0, min(offset, len(text)))) + 1


# ---------------------------------------------------------------------------
# 声明树
# ---------------------------------------------------------------------------


@dataclass
class _Annotation:
    name: str
    args: str
    line: int


@dataclass
class _Declaration:
    kind: str  # class | method | field | block
    head: str
    annotations: List[_Annotation] = field(default_factory=list)
    params: str = ""
    line: int = 0
    init: str = ""
    members: List["_Declaration"] = field(default_factory=list)


def _parse_annotation_at(text: str, index: int) -> Tuple[_Annotation, int]:
    """解析 ``@Xxx(...)``，返回 (注解, 结束下标)。"""
    match = re.match(r"@([A-Za-z_$][\w$.]*)", text[index:])
    if not match:
        return _Annotation(name="", args="", line=_line_of(text, index)), index + 1
    name = match.group(1).split(".")[-1]
    cursor = index + match.end()
    args = ""
    while cursor < len(text) and text[cursor] in " \t\r\n":
        cursor += 1
    if cursor < len(text) and text[cursor] == "(":
        end = _skip_balanced(text, cursor, "(", ")")
        args = text[cursor + 1 : max(cursor + 1, end - 1)]
        cursor = end
    return _Annotation(name=name, args=args, line=_line_of(text, index)), cursor


def _scan_declarations(text: str, start: int, end: int, depth: int = 0) -> List[_Declaration]:
    """顺序扫描声明（顺序扫描可避免把方法参数误判成字段声明）。"""
    declarations: List[_Declaration] = []
    i = start
    while i < end:
        while i < end and text[i] in " \t\r\n;":
            i += 1
        if i >= end:
            break
        annotations: List[_Annotation] = []
        while i < end and text[i] == "@":
            annotation, i = _parse_annotation_at(text, i)
            annotations.append(annotation)
            while i < end and text[i] in " \t\r\n":
                i += 1
        head_start = i
        j = i
        while j < end and text[j] not in "({;=":
            j += 1
        if j >= end:
            break
        head = text[head_start:j].strip()
        line = _line_of(text, head_start)
        if text[j] == "{":
            body_end = _skip_balanced(text, j, "{", "}")
            if re.search(r"\b(class|interface|enum)\b", head) and depth < MAX_NESTING:
                members = _scan_declarations(text, j + 1, max(j + 1, body_end - 1), depth + 1)
                declarations.append(
                    _Declaration(kind="class", head=head, annotations=annotations, line=line, members=members)
                )
            else:
                declarations.append(_Declaration(kind="block", head=head, annotations=annotations, line=line))
            i = body_end
            continue
        if text[j] == "(":
            params_end = _skip_balanced(text, j, "(", ")")
            cursor = params_end
            while cursor < end and text[cursor] in " \t\r\n":
                cursor += 1
            if text.startswith("throws", cursor):
                while cursor < end and text[cursor] not in "{;":
                    cursor += 1
            params = text[j + 1 : max(j + 1, params_end - 1)]
            if cursor < end and text[cursor] == "{":
                body_end = _skip_balanced(text, cursor, "{", "}")
                declarations.append(
                    _Declaration(kind="method", head=head, annotations=annotations, params=params, line=line)
                )
                i = body_end
                continue
            declarations.append(
                _Declaration(kind="method", head=head, annotations=annotations, params=params, line=line)
            )
            i = cursor + 1
            continue
        statement_end = _statement_end(text, j, end)
        declarations.append(
            _Declaration(
                kind="field",
                head=head,
                annotations=annotations,
                line=line,
                init=text[j:statement_end].strip(),
            )
        )
        i = statement_end + 1
    return declarations


# ---------------------------------------------------------------------------
# 注解参数
# ---------------------------------------------------------------------------


def _split_args(args: str) -> List[Tuple[Optional[str], str]]:
    """按顶层逗号切分注解参数，返回 [(键或 None, 值文本)]。"""
    result: List[Tuple[Optional[str], str]] = []
    if not args.strip():
        return result
    depth = 0
    current: List[str] = []
    for ch in args:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth <= 0:
            result.append(_split_kv("".join(current)))
            current = []
            continue
        current.append(ch)
    if "".join(current).strip():
        result.append(_split_kv("".join(current)))
    return result


def _split_kv(text: str) -> Tuple[Optional[str], str]:
    depth = 0
    for index, ch in enumerate(text):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "=" and depth <= 0:
            key = text[:index].strip()
            if re.match(r"^[A-Za-z_$][\w$]*$", key):
                return key, text[index + 1 :].strip()
    return None, text.strip()


def _arg_value(annotation: _Annotation, key: str = "value") -> Optional[str]:
    """取注解参数原始文本（位置参数按 ``value`` 处理）。"""
    positional: Optional[str] = None
    for name, value in _split_args(annotation.args):
        if name is None:
            if positional is None:
                positional = value
            continue
        if name == key:
            return value
    if key == "value":
        return positional
    return None


def _unescape_java(text: str) -> str:
    return (
        text.replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace("\\r", "\r")
        .replace('\\"', '"')
        .replace("\\'", "'")
        .replace("\\\\", "\\")
    )


_STRING_OR_SYMBOL_RE = re.compile(r'"((?:[^"\\]|\\.)*)"|([A-Za-z_$][\w$]*)(?:\s*\.\s*([A-Za-z_$][\w$]*))?')


def _resolve_string_expr(expr: Optional[str], symbols: Mapping[str, str], warnings: WarningCollector) -> str:
    """把 ``"a" + PREFIX + CONST.x`` 解析成字符串；未解析的常量记 warning。"""
    if expr is None:
        return ""
    text = expr.strip()
    if not text:
        return ""
    if text.startswith("{"):
        inner = text[1:-1] if text.endswith("}") else text[1:]
        parts = [_resolve_string_expr(item, symbols, warnings) for item in _split_items(inner)]
        return parts[0] if parts else ""
    out: List[str] = []
    for match in _STRING_OR_SYMBOL_RE.finditer(text):
        if match.group(1) is not None:
            out.append(_unescape_java(match.group(1)))
            continue
        name = match.group(2)
        owner = match.group(3)
        if name in _METHOD_KEYWORDS:
            continue
        qualified = "%s.%s" % (name, owner) if owner else name
        value = symbols.get(qualified)
        if value is None and owner:
            value = symbols.get(owner)
        if value is None:
            value = symbols.get(name)
        if value is None:
            warnings.add("存在未解析常量: %s" % qualified)
            continue
        out.append(value)
    return "".join(out)


def _split_items(text: str) -> List[str]:
    """按顶层逗号拆分数组字面量（保留引号，交给 _resolve_string_expr 解析）。"""
    items: List[str] = []
    depth = 0
    current: List[str] = []
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth <= 0:
            items.append("".join(current).strip())
            current = []
            continue
        current.append(ch)
    tail = "".join(current).strip()
    if tail:
        items.append(tail)
    return items


def _string_values(annotation: _Annotation, key: str, symbols: Mapping[str, str], warnings: WarningCollector) -> List[str]:
    """取注解的字符串数组/字符串参数，返回解析后的多值列表。"""
    raw = _arg_value(annotation, key)
    if raw is None:
        return []
    text = raw.strip()
    if text.startswith("{"):
        values = []
        for expr in _split_items(text[1:-1] if text.endswith("}") else text[1:]):
            value = _resolve_string_expr(expr, symbols, warnings)
            if value:
                values.append(value)
        return values
    value = _resolve_string_expr(text, symbols, warnings)
    return [value] if value else []


def _string_value(annotation: _Annotation, key: str, symbols: Mapping[str, str], warnings: WarningCollector) -> str:
    values = _string_values(annotation, key, symbols, warnings)
    return values[0] if values else ""


def _bool_value(annotation: _Annotation, key: str) -> Optional[bool]:
    raw = _arg_value(annotation, key)
    if raw is None:
        return None
    text = raw.strip().lower()
    if text in ("true", "false"):
        return text == "true"
    return None


def _find_annotation(annotations: Sequence[_Annotation], names: Sequence[str]) -> Optional[_Annotation]:
    for annotation in annotations:
        if annotation.name in names:
            return annotation
    return None


# ---------------------------------------------------------------------------
# Java 类型
# ---------------------------------------------------------------------------


def _strip_generics(java_type: str) -> str:
    text = re.sub(r"<.*>", "", java_type, flags=re.S).strip()
    text = text.replace("...", "").replace("[]", "").strip()
    return text


def _unwrap_generic(java_type: str) -> List[str]:
    """取泛型参数：``List<UserDto>`` → ["UserDto"]。"""
    match = re.search(r"<(.+)>", java_type, re.S)
    if not match:
        return []
    parts: List[str] = []
    depth = 0
    current: List[str] = []
    for ch in match.group(1):
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        if ch == "," and depth <= 0:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(ch)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def _simple_name(java_type: str) -> str:
    return _strip_generics(java_type).split(".")[-1].strip()


def _json_type_of(java_type: str) -> str:
    name = _simple_name(java_type).lower()
    if name.endswith("[]"):
        return "array"
    return _JAVA_TYPE_JSON.get(name, "object")


def _is_array_type(java_type: str) -> bool:
    name = _simple_name(java_type).lower()
    return name in ("list", "set", "collection", "iterable", "arraylist", "linkedlist") or java_type.strip().endswith("[]")


def _is_map_type(java_type: str) -> bool:
    return _simple_name(java_type).lower() in ("map", "hashmap", "linkedhashmap", "treemap", "jsonobject")


def _force_format(java_type: str) -> str:
    return _JAVA_TYPE_FORMAT.get(_simple_name(java_type).lower(), "")


# ---------------------------------------------------------------------------
# 扫描上下文
# ---------------------------------------------------------------------------


@dataclass
class _Dto:
    name: str
    package: str
    file_ref: str
    fields: List[_Declaration] = field(default_factory=list)


@dataclass
class _Config:
    context_path: str = ""
    application_name: str = ""
    port: Optional[int] = None


class _SpringScanner:
    def __init__(self, root: Path, options: Any, warnings: WarningCollector) -> None:
        self.root = root
        self.options = options
        self.warnings = warnings
        self.endpoints: List[EndpointSpec] = []
        self.symbols: Dict[str, str] = {}
        self.file_symbols: Dict[str, Dict[str, str]] = {}
        self.dtos: Dict[str, List[_Dto]] = {}
        self.config = _Config()
        self.skipped = 0

    # -- 采集阶段：常量、DTO、配置
    def collect(self, files: Sequence[Tuple[str, Path]]) -> Dict[str, str]:
        texts: Dict[str, str] = {}
        for ref, path in files:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                self.warnings.add("无法读取文件 %s: %s" % (ref, exc))
                continue
            if "\ufffd" in text:
                self.warnings.add("文件存在无法解码的字符，已按替换字符处理: %s" % ref)
            cleaned = _strip_comments(text)
            texts[ref] = cleaned
            self.file_symbols[ref] = self._collect_symbols(cleaned, ref)
        for mapping in self.file_symbols.values():
            for key, value in mapping.items():
                self.symbols.setdefault(key, value)
        for ref, cleaned in texts.items():
            self._collect_dtos(cleaned, ref)
        return texts

    def _collect_symbols(self, text: str, ref: str) -> Dict[str, str]:
        """文件级 Java 字符串常量符号表：NAME → 值，同时记录 Class.NAME。"""
        symbols: Dict[str, str] = {}
        classes = _scan_declarations(text, 0, len(text))
        for cls in classes:
            if cls.kind != "class":
                continue
            class_name = _class_name_of(cls.head)
            for member in cls.members:
                if member.kind != "field":
                    continue
                if "static" not in member.head or "final" not in member.head:
                    continue
                name_match = re.search(r"([A-Za-z_$][\w$]*)\s*$", member.head)
                if not name_match:
                    continue
                name = name_match.group(1)
                value_expr = member.init.lstrip("=").strip()
                value = _resolve_string_expr(value_expr, symbols, WarningCollector())
                if value:
                    symbols[name] = value
                    if class_name:
                        symbols["%s.%s" % (class_name, name)] = value
        return symbols

    def _collect_dtos(self, text: str, ref: str) -> None:
        package = ""
        package_match = re.search(r"\bpackage\s+([\w.]+)\s*;", text)
        if package_match:
            package = package_match.group(1)
        for cls in _scan_declarations(text, 0, len(text)):
            if cls.kind != "class":
                continue
            name = _class_name_of(cls.head)
            if not name:
                continue
            fields = [member for member in cls.members if member.kind == "field" and "static" not in member.head]
            if not fields:
                continue
            dto = _Dto(name=name, package=package, file_ref=ref, fields=fields)
            self.dtos.setdefault(name, []).append(dto)

    # -- 配置
    def load_config(self, files: Sequence[Tuple[str, Path]]) -> None:
        config = _Config()
        for ref, path in files:
            try:
                text = path.read_text(encoding="utf-8-sig", errors="replace")
            except OSError as exc:
                self.warnings.add("无法读取配置文件 %s: %s" % (ref, exc))
                continue
            data: Dict[str, Any] = {}
            if path.suffix.lower() in (".yml", ".yaml"):
                try:
                    loaded = load_yaml(text)
                    if isinstance(loaded, Mapping):
                        data = dict(loaded)
                except Exception as exc:  # YAML 解析失败不应中断扫描
                    self.warnings.add("配置文件解析失败 %s: %s" % (ref, exc))
                    continue
            else:
                data = _parse_properties(text)
            context = (
                _nested_get(data, ("server", "servlet", "context-path"))
                or _nested_get(data, ("server", "context-path"))
                or _flat_key(data, "server.servlet.context-path", "server.context-path")
            )
            if context:
                config.context_path = text_of(context)
            name = _nested_get(data, ("spring", "application", "name")) or _flat_key(data, "spring.application.name")
            if name:
                config.application_name = text_of(name)
            port = _nested_get(data, ("server", "port"))
            if port is None:
                port = _flat_key(data, "server.port")
            if port is not None:
                try:
                    config.port = int(str(port))
                except (TypeError, ValueError):
                    self.warnings.add("server.port 不是合法整数: %s" % port)
        self.config = config
        if config.application_name:
            self.warnings.add("detected_application_name: %s" % config.application_name)
        if config.port is not None:
            self.warnings.add("detected_server_port: %d" % config.port)
        if config.context_path and config.context_path not in ("", "/"):
            self.warnings.add("detected_context_path: %s" % config.context_path)

    # -- 扫描入口
    def scan_file(self, cleaned: str, ref: str) -> None:
        symbols = dict(self.symbols)
        symbols.update(self.file_symbols.get(ref, {}))
        for cls in _scan_declarations(cleaned, 0, len(cleaned)):
            if cls.kind != "class":
                continue
            self._scan_controller(cls, cleaned, ref, symbols)

    def _scan_controller(self, cls: _Declaration, text: str, ref: str, symbols: Mapping[str, str]) -> None:
        class_annotations = {annotation.name for annotation in cls.annotations}
        is_controller = bool(class_annotations & {"RestController", "Controller"})
        methods = [member for member in cls.members if member.kind == "method"]
        has_mapping = any(
            _find_annotation(member.annotations, list(MAPPING_ANNOTATIONS)) for member in methods
        )
        if not is_controller and not has_mapping:
            return
        class_name = _class_name_of(cls.head) or "?"
        base_paths = [""]
        request_mapping = _find_annotation(cls.annotations, ["RequestMapping"])
        if request_mapping is not None:
            paths = _string_values(request_mapping, "value", symbols, self.warnings)
            if not paths:
                paths = _string_values(request_mapping, "path", symbols, self.warnings)
            if paths:
                base_paths = paths
        elif has_mapping:
            self.warnings.add("类 %s 未声明类级 @RequestMapping，路径以方法映射为准" % class_name)
        class_tags = self._class_tags(cls, symbols)
        class_description = self._class_description(cls, symbols)

        for member in methods:
            mapping = _find_annotation(member.annotations, list(MAPPING_ANNOTATIONS))
            if mapping is None:
                continue
            self._build_endpoints(cls, member, mapping, base_paths, class_tags, class_description, symbols, ref)

    def _class_tags(self, cls: _Declaration, symbols: Mapping[str, str]) -> List[str]:
        tags: List[str] = []
        api = _find_annotation(cls.annotations, ["Api", "Tag"])
        if api is not None:
            values = _string_values(api, "tags", symbols, self.warnings)
            if not values:
                values = _string_values(api, "tag", symbols, self.warnings)
            for value in values:
                for part in value.split(","):
                    part = part.strip()
                    if part and part not in tags:
                        tags.append(part)
            if not values:
                name = _string_value(api, "name", symbols, self.warnings)
                if name:
                    tags.append(name)
        return tags

    def _class_description(self, cls: _Declaration, symbols: Mapping[str, str]) -> str:
        api = _find_annotation(cls.annotations, ["Api", "Tag"])
        if api is None:
            return ""
        return _string_value(api, "description", symbols, self.warnings) or _string_value(
            api, "value", symbols, self.warnings
        )

    # -- 单方法 → 接口
    def _build_endpoints(
        self,
        cls: _Declaration,
        member: _Declaration,
        mapping: _Annotation,
        base_paths: Sequence[str],
        class_tags: Sequence[str],
        class_description: str,
        symbols: Mapping[str, str],
        ref: str,
    ) -> None:
        method = MAPPING_ANNOTATIONS.get(mapping.name, "")
        if not method:
            methods = _string_values(mapping, "method", symbols, WarningCollector())
            method = _method_from_request_methods(mapping.args)
            if not method:
                method = "GET"
                self.warnings.add("方法 %s 未指定 HTTP 方法，默认 GET" % _method_name_of(member.head))
        method_paths = _string_values(mapping, "value", symbols, self.warnings)
        if not method_paths:
            method_paths = _string_values(mapping, "path", symbols, self.warnings)
        if not method_paths:
            method_paths = [""]
        deprecated = bool(_find_annotation(member.annotations, ["Deprecated"])) or bool(
            _find_annotation(cls.annotations, ["Deprecated"])
        )
        if deprecated and not bool(opt(self.options, "include_deprecated", False)):
            self.skipped += 1
            self.warnings.add(
                "已跳过过时接口: %s %s"
                % (method, _join_paths(self.config.context_path, base_paths[0] if base_paths else "", method_paths[0]))
            )
            return
        operation = _find_annotation(member.annotations, ["ApiOperation", "Operation"])
        summary = ""
        description = class_description
        tags = list(class_tags)
        if operation is not None:
            summary = _string_value(operation, "value", symbols, self.warnings) or _string_value(
                operation, "summary", symbols, self.warnings
            )
            notes = _string_value(operation, "notes", symbols, self.warnings) or _string_value(
                operation, "description", symbols, self.warnings
            )
            if notes:
                description = notes
            extra_tags = _string_values(operation, "tags", symbols, self.warnings)
            for value in extra_tags:
                for part in value.split(","):
                    part = part.strip()
                    if part and part not in tags:
                        tags.append(part)
        consumes = _string_values(mapping, "consumes", symbols, self.warnings)
        params, body_type, body_example = self._build_params(member, symbols)
        if body_type == "none" and method in ("POST", "PUT", "PATCH") and _is_form_media(consumes):
            query_params = [spec for spec in params if spec.location == "query"]
            if query_params:
                body_type = "multipart" if "multipart" in consumes[0].lower() else "form"
                body_example = {spec.name: value_to_text(spec.example) for spec in query_params}
                params = [
                    ParamSpec(
                        name=spec.name,
                        location="body",
                        required=spec.required,
                        type=spec.type,
                        example=spec.example,
                        default=spec.default,
                        description=spec.description,
                        enum_values=list(spec.enum_values),
                    )
                    if spec.location == "query"
                    else spec
                    for spec in params
                ]

        for base_path in base_paths:
            for method_path in method_paths:
                full_path = _join_paths(self.config.context_path, base_path, method_path)
                request = RequestSpec(method=method, path=full_path, base_url=None)
                path_values: Dict[str, str] = {}
                param_specs: List[ParamSpec] = []
                for spec in params:
                    param_specs.append(spec)
                    placeholder = value_to_text(spec.example)
                    if spec.location == "path":
                        path_values[spec.name] = placeholder or "1"
                    elif spec.location == "query":
                        request.query.append(
                            KeyValue(name=spec.name, value=placeholder, enabled=spec.required,
                                     description=spec.description, required=spec.required)
                        )
                    elif spec.location == "header":
                        request.headers.append(
                            KeyValue(name=spec.name, value=placeholder, enabled=spec.required,
                                     description=spec.description, required=spec.required)
                        )
                    elif spec.location == "cookie":
                        request.cookies.append(
                            KeyValue(name=spec.name, value=placeholder, enabled=spec.required,
                                     description=spec.description, required=spec.required)
                        )
                template = _normalize_path_params(full_path)
                request.path = _substitute_path(template, path_values)
                if body_type != "none":
                    request.body_type = body_type
                    request.body = body_example
                    header = "application/json"
                    if body_type == "multipart":
                        header = "multipart/form-data"
                    elif body_type == "form":
                        header = "application/x-www-form-urlencoded"
                    if consumes:
                        preferred = consumes[0].split(";")[0].strip()
                        if preferred:
                            header = preferred
                    request.headers.append(KeyValue(name="Content-Type", value=header, enabled=True))
                name = summary or _method_name_of(member.head) or "%s %s" % (method, template)
                endpoint = EndpointSpec(
                    name=name,
                    folder=join_folder(
                        opt(self.options, "folder_prefix", ""), tags[0] if tags else _class_name_of(cls.head)
                    ),
                    method=method,
                    path=template,
                    summary=summary,
                    description=description,
                    tags=list(tags)
                    + [text_of(tag) for tag in as_list(opt(self.options, "tags", [])) if text_of(tag) and tag not in tags],
                    params=param_specs,
                    request=request,
                    source="spring",
                    source_ref=ref,
                    operation_id=_method_name_of(member.head),
                    deprecated=deprecated,
                )
                apply_default_headers(endpoint.request, self.options)
                self.endpoints.append(endpoint)

    def _build_params(
        self, member: _Declaration, symbols: Mapping[str, str]
    ) -> Tuple[List[ParamSpec], str, Any]:
        specs: List[ParamSpec] = []
        body_type = "none"
        body: Any = None
        method_name = _method_name_of(member.head)
        for raw_param in _split_annotation_params(member.params):
            annotations, java_type, java_name = _parse_java_param(raw_param)
            if not java_name and not annotations:
                continue
            api_param = _find_annotation(annotations, ["ApiParam", "Parameter"])
            annotation = _find_annotation(annotations, list(_BINDING_ANNOTATIONS))
            kind = annotation.name if annotation is not None else ""
            description = ""
            example: Any = None
            required = False
            default: Any = None
            if api_param is not None:
                description = _string_value(api_param, "value", symbols, self.warnings) or _string_value(
                    api_param, "description", symbols, self.warnings
                )
                example = _string_value(api_param, "example", symbols, WarningCollector()) or None
                flag = _bool_value(api_param, "required")
                if flag is not None:
                    required = flag
            for marker in _REQUIRED_ANNOTATIONS:
                if _find_annotation(annotations, [marker]) is not None:
                    required = True
            if kind in ("RequestBody", "RequestPart", "ModelAttribute"):
                if kind == "RequestPart":
                    body_type = "multipart"
                    body = {java_name or "file": ""}
                else:
                    body_type = "json"
                    body = self._body_example(java_type, seen=())
                dto = self._find_dto(java_type)
                if dto is not None:
                    specs.extend(self._dto_params(dto))
                if kind == "ModelAttribute":
                    self.warnings.add("ModelAttribute 参数按 json 请求体处理: %s" % (java_name or java_type))
                if annotation is not None:
                    flag = _bool_value(annotation, "required")
                    if flag is not None:
                        required = flag
                continue
            name = ""
            if annotation is not None:
                name = _string_value(annotation, "value", symbols, self.warnings) or _string_value(
                    annotation, "name", symbols, self.warnings
                )
                default = _string_value(annotation, "defaultValue", symbols, WarningCollector()) or None
                flag = _bool_value(annotation, "required")
                if flag is not None:
                    required = flag
            name = name or java_name
            if not name:
                self.warnings.add("方法 %s 存在无法解析的参数，已跳过" % method_name)
                continue
            location = {
                "PathVariable": "path",
                "RequestParam": "query",
                "RequestHeader": "header",
                "CookieValue": "cookie",
            }.get(kind, "query")
            if kind == "" and _json_type_of(java_type) == "object" and not _is_map_type(java_type):
                location = "body"
                if body_type == "none":
                    body_type = "form"
                    body = {name: ""}
                self.warnings.add("未标注注解的复杂参数按请求体处理: %s" % name)
            json_type = _json_type_of(java_type)
            if json_type == "object" and not _is_map_type(java_type) and location != "body":
                dto = self._find_dto(java_type)
                if dto is not None:
                    self.warnings.add("参数 %s 是自定义类型，已按 object 处理" % name)
            if required is False and kind == "PathVariable":
                required = True
            if location == "path":
                required = True
            sample = example if example is not None else default
            if sample is None:
                sample = self._sample_for_java_type(java_type)
                if location == "path" and sample in ("", None):
                    sample = "1"
            if is_secret_key(name):
                sample = ""
                self.warnings.add("敏感参数已留空: %s" % name)
            specs.append(
                ParamSpec(
                    name=name,
                    location=location,
                    required=required,
                    type=json_type,
                    example=sample,
                    default=default,
                    description=description,
                )
            )
        return specs, body_type, body

    def _warn_missing_type(self, java_type: str) -> None:
        name = _simple_name(java_type)
        if not name or name[0].islower():
            return
        if name.lower() in _JAVA_TYPE_JSON:
            return
        self.warnings.add("未找到类型 %s 的字段定义，已按 object 生成空对象" % name)

    def _dto_params(self, dto: _Dto) -> List[ParamSpec]:
        """DTO 字段 → ``location="body"`` 参数元数据。"""
        specs: List[ParamSpec] = []
        for field_decl in dto.fields:
            name = _field_name_of(field_decl.head)
            if not name:
                continue
            java_type = _field_type_of(field_decl.head)
            example: Any = None
            description = ""
            required = False
            for marker in ("ApiModelProperty", "Schema"):
                annotation = _find_annotation(field_decl.annotations, [marker])
                if annotation is None:
                    continue
                description = _string_value(annotation, "value", {}, self.warnings) or _string_value(
                    annotation, "description", {}, self.warnings
                )
                example = _string_value(annotation, "example", {}, WarningCollector()) or None
                flag = _bool_value(annotation, "required")
                if flag is not None:
                    required = flag
            for marker in _REQUIRED_ANNOTATIONS:
                if _find_annotation(field_decl.annotations, [marker]) is not None:
                    required = True
            if is_secret_key(name):
                example = ""
            specs.append(
                ParamSpec(
                    name=name,
                    location="body",
                    required=required,
                    type=_json_type_of(java_type),
                    example=example if example is not None else self._sample_for_java_type(java_type),
                    description=description,
                )
            )
        return specs

    def _sample_for_java_type(self, java_type: str) -> Any:
        if _is_array_type(java_type):
            inner = (_unwrap_generic(java_type) or [""])[0]
            return [self._sample_for_java_type(inner)] if inner else []
        if _is_map_type(java_type):
            return {}
        dto = self._find_dto(java_type)
        if dto is not None:
            return self._dto_example(dto, seen=())
        self._warn_missing_type(java_type)
        return sample_by_type(_json_type_of(java_type), _force_format(java_type))

    def _body_example(self, java_type: str, seen: Tuple[str, ...]) -> Any:
        if _is_array_type(java_type):
            inner = (_unwrap_generic(java_type) or [""])[0]
            return [self._body_example(inner, seen)] if inner else []
        if _is_map_type(java_type):
            return {}
        dto = self._find_dto(java_type)
        if dto is None:
            kind = _json_type_of(java_type)
            if kind == "object":
                self.warnings.add("未找到请求体类型 %s 的字段定义，已生成空对象" % _simple_name(java_type))
                return {}
            return sample_by_type(kind, _force_format(java_type))
        return self._dto_example(dto, seen=seen)

    def _dto_example(self, dto: _Dto, seen: Tuple[str, ...], depth: int = 0) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        if depth > 3 or dto.name in seen:
            return result
        chain = seen + (dto.name,)
        for field_decl in dto.fields:
            field_name = _field_name_of(field_decl.head)
            if not field_name:
                continue
            java_type = _field_type_of(field_decl.head)
            example: Any = None
            description = ""
            required = False
            for marker in ("ApiModelProperty", "Schema"):
                annotation = _find_annotation(field_decl.annotations, [marker])
                if annotation is None:
                    continue
                description = _string_value(annotation, "value", {}, self.warnings) or _string_value(
                    annotation, "description", {}, self.warnings
                )
                example = _string_value(annotation, "example", {}, WarningCollector()) or None
                flag = _bool_value(annotation, "required")
                if flag is not None:
                    required = flag
            for marker in _REQUIRED_ANNOTATIONS:
                if _find_annotation(field_decl.annotations, [marker]) is not None:
                    required = True
            if is_secret_key(field_name):
                result[field_name] = ""
                self.warnings.add("敏感字段已留空: %s" % field_name)
                continue
            if example is not None:
                result[field_name] = example
                continue
            if _is_array_type(java_type):
                inner = (_unwrap_generic(java_type) or [""])[0]
                if inner:
                    inner_dto = self._find_dto(inner)
                    if inner_dto is not None:
                        result[field_name] = [self._dto_example(inner_dto, chain, depth + 1)]
                    else:
                        self._warn_missing_type(inner)
                        result[field_name] = [sample_by_type(_json_type_of(inner), _force_format(inner))]
                else:
                    result[field_name] = []
                continue
            if _is_map_type(java_type):
                result[field_name] = {}
                continue
            inner_dto = self._find_dto(java_type)
            if inner_dto is not None and depth < 3:
                result[field_name] = self._dto_example(inner_dto, chain, depth + 1)
                continue
            result[field_name] = self._sample_for_java_type(java_type)
        return result

    def _find_dto(self, java_type: str) -> Optional[_Dto]:
        name = _simple_name(java_type)
        candidates = self.dtos.get(name)
        if not candidates:
            return None
        return sorted(candidates, key=lambda item: (item.package, item.file_ref))[0]


def _flat_key(data: Mapping[str, Any], *keys: str) -> Any:
    """读取 .properties 的扁平点号键（如 ``server.servlet.context-path``）。"""
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def _nested_get(data: Mapping[str, Any], keys: Sequence[str]) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
        if current is None:
            return None
    return current


def _parse_properties(text: str) -> Dict[str, Any]:
    """解析 .properties（支持 ``=`` / ``:`` 分隔与 ``#``/``!`` 注释）。"""
    result: Dict[str, Any] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "!")):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
        elif ":" in line:
            key, _, value = line.partition(":")
        else:
            continue
        key = key.strip()
        if key:
            result[key] = value.strip()
    return result


def _is_form_media(consumes: Sequence[str]) -> bool:
    for item in consumes:
        kind = text_of(item).split(";")[0].strip().lower()
        if kind in ("application/x-www-form-urlencoded", "multipart/form-data"):
            return True
    return False


def _method_from_request_methods(args: str) -> str:
    match = re.search(r"RequestMethod\s*\.\s*([A-Z]+)", args)
    if match:
        method = match.group(1).upper()
        return method if method in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS") else ""
    return ""


def _class_name_of(head: str) -> str:
    match = re.search(r"\b(?:class|interface|enum)\s+([A-Za-z_$][\w$]*)", head)
    return match.group(1) if match else ""


def _method_name_of(head: str) -> str:
    match = re.search(r"([A-Za-z_$][\w$]*)\s*$", head)
    return match.group(1) if match else ""


def _field_name_of(head: str) -> str:
    match = re.search(r"([A-Za-z_$][\w$]*)\s*$", head)
    return match.group(1) if match else ""


def _field_type_of(head: str) -> str:
    text = re.sub(r"^(?:public|private|protected|static|final|transient|volatile|\s)+", "", head).strip()
    match = re.match(r"^(.*?)\s+[A-Za-z_$][\w$]*$", text, re.S)
    return match.group(1).strip() if match else text


def _split_annotation_params(params: str) -> List[str]:
    """按顶层逗号拆分方法参数列表。"""
    result: List[str] = []
    depth = 0
    current: List[str] = []
    for ch in params:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth <= 0:
            result.append("".join(current).strip())
            current = []
            continue
        current.append(ch)
    tail = "".join(current).strip()
    if tail:
        result.append(tail)
    return result


def _parse_java_param(text: str) -> Tuple[List[_Annotation], str, str]:
    """解析一个方法参数：注解列表 + Java 类型 + 参数名。"""
    annotations: List[_Annotation] = []
    i = 0
    length = len(text)
    while i < length:
        while i < length and text[i] in " \t\r\n":
            i += 1
        if i < length and text[i] == "@":
            annotation, i = _parse_annotation_at(text, i)
            annotations.append(annotation)
            continue
        break
    rest = text[i:].strip()
    rest = re.sub(r"^(?:final)\s+", "", rest)
    match = re.match(r"^(.*?)([A-Za-z_$][\w$]*)\s*$", rest, re.S)
    if not match:
        return annotations, rest, ""
    java_type = match.group(1).strip()
    java_name = match.group(2).strip()
    return annotations, java_type, java_name


def _join_paths(*parts: str) -> str:
    segments: List[str] = []
    for part in parts:
        text = text_of(part).strip()
        if not text or text == "/":
            continue
        segments.append(text.strip("/"))
    joined = "/".join(segment for segment in segments if segment)
    return "/" + joined if joined else "/"


def _normalize_path_params(path: str) -> str:
    """``{id:\\\\d+}`` → ``{id}``。"""
    return re.sub(r"\{([^}:]+):[^}]+\}", r"{\1}", path)


def _substitute_path(path: str, values: Mapping[str, str]) -> str:
    def replace(match: "re.Match[str]") -> str:
        return values.get(match.group(1), match.group(0))

    return re.sub(r"\{([^}/]+)\}", replace, path)


def _iter_files(root: Path, patterns: Sequence[str]) -> List[Tuple[str, Path]]:
    result: List[Tuple[str, Path]] = []
    if root.is_file():
        candidates = [root]
    else:
        candidates = []
        for pattern in patterns:
            candidates.extend(sorted(root.rglob(pattern)))
    for path in candidates:
        try:
            relative = path.relative_to(root if root.is_dir() else root.parent)
        except ValueError:
            relative = Path(path.name)
        ref = str(relative).replace("\\", "/")
        if any(part in SKIP_DIRS for part in Path(ref).parts):
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        result.append((ref, path))
    return sorted(result, key=lambda item: item[0])


def scan_spring_project(root: Path, options: Any = None) -> ImportResult:
    """扫描 Spring 项目源码，产出接口与冒烟用例。"""
    warnings = WarningCollector()
    target = Path(root)
    result = ImportResult(source="spring")
    result.stats = ImportStats(source="spring", source_ref=str(root))
    if not target.exists():
        raise ValueError("扫描路径不存在: %s" % root)

    java_files = _iter_files(target, ("*.java",))
    config_files = _iter_files(target, ("application*.yml", "application*.yaml", "application*.properties"))
    scanner = _SpringScanner(target, options, warnings)
    configs = [item for item in config_files if _is_application_config(item[0])]
    configs.sort(key=lambda item: (0 if _is_base_config(item[0]) else 1, item[0]))
    scanner.load_config(configs)
    texts = scanner.collect(java_files)
    for ref, cleaned in texts.items():
        try:
            scanner.scan_file(cleaned, ref)
        except Exception as exc:  # 任何单文件问题都不应中断导入
            warnings.add("解析文件失败 %s: %s" % (ref, exc))

    result.endpoints = scanner.endpoints
    cases = generate_cases(result.endpoints, options)
    result.cases = cases
    result.title = scanner.config.application_name or target.name
    result.stats.created = len(result.endpoints)
    result.stats.cases_created = len(cases)
    result.stats.skipped = scanner.skipped
    result.stats.warnings = warnings.items
    return result


def _is_base_config(ref: str) -> bool:
    """``application.yml`` / ``application.properties`` 为基础配置，其余为 profile 配置。"""
    name = ref.rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0]
    return stem == "application"


def _is_application_config(ref: str) -> bool:
    name = ref.rsplit("/", 1)[-1]
    return name.startswith("application") and name.split(".")[-1] in ("yml", "yaml", "properties")
