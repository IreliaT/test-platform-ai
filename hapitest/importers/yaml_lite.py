"""YAML 子集解析器（零第三方依赖，优先使用 PyYAML）。

导入器需要读取 OpenAPI / Spring 的 ``*.yml``，但平台约定「只依赖标准库」，
因此这里提供两种实现：

* :func:`load_yaml` —— 已安装 PyYAML 时用 ``yaml``（并关闭 timestamp 隐式解析，
  保证 ``2024-01-01`` 这类标量始终是字符串，导入结果可复现）；否则回退到内置解析器。
* :func:`load_yaml_lite` —— 始终使用内置解析器，便于测试与跨环境一致性。

内置解析器覆盖的 YAML 子集：

* 块映射、块序列、任意层级缩进；
* 流式集合 ``{}`` / ``[]``（允许跨行）；
* 单引号 / 双引号标量（含 ``\\n`` 等转义、``''`` 转义、跨行折叠）；
* 普通标量按 int / float / bool / null 强制转换（与 PyYAML 的 YAML 1.1 规则一致）；
* 注释、``---`` 文档开始、``%`` 指令、``...`` 文档结束；
* 字面块 ``|`` 与折叠块 ``>``，支持 ``-`` / ``+`` 与显式缩进指示符；
* 多行普通标量（折叠为空格）。

明确不支持（遇到即抛 :class:`YamlLiteError` 并带上行号）：锚点 ``&``、别名 ``*``、
标签 ``!!``、合并键 ``<<``、复杂键 ``?``、制表符缩进。多文档 YAML 只返回第一个文档，
与 ``yaml.safe_load`` 行为一致。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, List, Optional, Tuple

__all__ = [
    "YamlLiteError",
    "HAS_PYYAML",
    "load_yaml",
    "load_yaml_lite",
    "load_file",
    "parse_scalar",
]

try:  # 可选依赖：已安装则使用，未安装则降级到内置解析器
    import yaml as _pyyaml
except Exception:  # pragma: no cover - 取决于运行环境
    _pyyaml = None

HAS_PYYAML = _pyyaml is not None


class YamlLiteError(ValueError):
    """YAML 解析错误。``line`` 为 1 起始的行号（0 表示无法定位）。"""

    def __init__(self, message: str, line: int = 0) -> None:
        self.message = str(message)
        self.line = int(line or 0)
        if self.line:
            text = "第 %d 行: %s" % (self.line, self.message)
        else:
            text = self.message
        super().__init__(text)


# ---------------------------------------------------------------------------
# 标量
# ---------------------------------------------------------------------------

_NULL_WORDS = frozenset(["", "~", "null", "Null", "NULL"])
_TRUE_WORDS = frozenset(["true", "True", "TRUE", "yes", "Yes", "YES", "on", "On", "ON"])
_FALSE_WORDS = frozenset(["false", "False", "FALSE", "no", "No", "NO", "off", "Off", "OFF"])

_INT_RE = re.compile(r"^[-+]?[0-9]+$")
_HEX_RE = re.compile(r"^[-+]?0x[0-9a-fA-F]+$")
_OCT_RE = re.compile(r"^[-+]?0o[0-7]+$")
_FLOAT_RE = re.compile(r"^[-+]?(\.[0-9]+|[0-9]+\.?[0-9]*)([eE][-+]?[0-9]+)?$")
_SPECIAL_FLOAT = {".inf": float("inf"), "+.inf": float("inf"), "-.inf": float("-inf")}


def parse_scalar(text: str) -> Any:
    """把普通标量文本转换为 int/float/bool/None/str。"""
    value = str(text).strip()
    if value in _NULL_WORDS:
        return None
    if value in _TRUE_WORDS:
        return True
    if value in _FALSE_WORDS:
        return False
    if value in _SPECIAL_FLOAT:
        return _SPECIAL_FLOAT[value]
    if value.lower() in (".nan", "+.nan", "-.nan"):
        return float("nan")
    compact = value.replace("_", "")
    if _INT_RE.match(compact):
        try:
            return int(compact)
        except ValueError:
            return value
    if _HEX_RE.match(compact) or _OCT_RE.match(compact):
        try:
            return int(compact, 16) if compact.lower().lstrip("+-").startswith("0x") else int(compact, 8)
        except ValueError:
            return value
    if _FLOAT_RE.match(compact) and ("." in compact or "e" in compact or "E" in compact):
        try:
            return float(compact)
        except ValueError:
            return value
    return value


_ESCAPES = {
    "0": "\0",
    "a": "\a",
    "b": "\b",
    "t": "\t",
    "n": "\n",
    "v": "\v",
    "f": "\f",
    "r": "\r",
    "e": "\x1b",
    " ": " ",
    '"': '"',
    "/": "/",
    "\\": "\\",
    "N": "\x85",
    "_": "\xa0",
    "L": "\u2028",
    "P": "\u2029",
}


def _decode_escape(text: str, index: int, line: int) -> Tuple[str, int]:
    """解析反斜杠转义；``index`` 指向反斜杠后的第一个字符。"""
    if index >= len(text):
        raise YamlLiteError("转义序列不完整", line)
    ch = text[index]
    if ch in _ESCAPES:
        return _ESCAPES[ch], index + 1
    widths = {"x": 2, "u": 4, "U": 8}
    if ch in widths:
        width = widths[ch]
        digits = text[index + 1 : index + 1 + width]
        if len(digits) != width:
            raise YamlLiteError("转义序列不完整: \\%s" % ch, line)
        try:
            return chr(int(digits, 16)), index + 1 + width
        except ValueError:
            raise YamlLiteError("非法的 \\%s 转义: %s" % (ch, digits), line)
    raise YamlLiteError("不支持的转义序列: \\%s" % ch, line)


def _strip_comment(text: str) -> str:
    """去掉普通标量末尾的注释（``#`` 前必须是空白或行首）。"""
    for index, ch in enumerate(text):
        if ch == "#" and (index == 0 or text[index - 1] in " \t"):
            return text[:index]
    return text.strip()


def _fold_lines(lines: List[Optional[str]]) -> str:
    """按 YAML 折叠规则连接多行文本；``None`` 表示空行。"""
    out: List[str] = []
    breaks = 0
    first = True
    for item in lines:
        if item is None:
            breaks += 1
            continue
        if not first:
            total = breaks + 1
            out.append(" " if total == 1 else "\n" * (total - 1))
        out.append(item)
        first = False
        breaks = 0
    return "".join(out)


# ---------------------------------------------------------------------------
# 流式集合
# ---------------------------------------------------------------------------


class _FlowParser:
    """解析 ``[a, b]`` / ``{k: v}`` 这类流式集合。"""

    def __init__(self, text: str, line: int) -> None:
        self.text = text
        self.pos = 0
        self.line = line

    def parse(self) -> Any:
        self._skip()
        if self._peek() == "":
            raise YamlLiteError("flow 集合为空", self.line)
        value = self._value()
        self._skip()
        rest = self.text[self.pos :].strip()
        if rest and not rest.startswith("#"):
            raise YamlLiteError("flow 集合之后存在多余内容: %s" % rest, self.line)
        return value

    # -- 内部
    def _peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def _skip(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos] in " \t":
            self.pos += 1

    def _value(self) -> Any:
        ch = self._peek()
        if ch == "[":
            return self._sequence()
        if ch == "{":
            return self._mapping()
        if ch in "\"'":
            return self._quoted(ch)
        raw = self._plain()
        if raw == "":
            raise YamlLiteError("flow 集合中出现空标量", self.line)
        return parse_scalar(raw)

    def _sequence(self) -> List[Any]:
        self.pos += 1
        items: List[Any] = []
        while True:
            self._skip()
            ch = self._peek()
            if ch == "":
                raise YamlLiteError("flow 序列缺少 ']'", self.line)
            if ch == "]":
                self.pos += 1
                return items
            items.append(self._value())
            self._skip()
            ch = self._peek()
            if ch == ",":
                self.pos += 1
                continue
            if ch == "]":
                self.pos += 1
                return items
            raise YamlLiteError("flow 序列缺少 ',' 或 ']'", self.line)

    def _mapping(self) -> dict:
        self.pos += 1
        result: dict = {}
        while True:
            self._skip()
            ch = self._peek()
            if ch == "":
                raise YamlLiteError("flow 映射缺少 '}'", self.line)
            if ch == "}":
                self.pos += 1
                return result
            key = self._key()
            self._skip()
            if self._peek() != ":":
                raise YamlLiteError("flow 映射缺少 ':'", self.line)
            self.pos += 1
            self._skip()
            if self._peek() in (",", "}"):
                value: Any = None
            else:
                value = self._value()
            result[key] = value
            self._skip()
            ch = self._peek()
            if ch == ",":
                self.pos += 1
                continue
            if ch == "}":
                self.pos += 1
                return result
            raise YamlLiteError("flow 映射缺少 ',' 或 '}'", self.line)

    def _key(self) -> Any:
        self._skip()
        if self._peek() in "\"'":
            return self._quoted(self._peek())
        start = self.pos
        while self.pos < len(self.text):
            ch = self.text[self.pos]
            if ch == ":":
                nxt = self.text[self.pos + 1] if self.pos + 1 < len(self.text) else " "
                if nxt in " \t,}]" or self.pos + 1 >= len(self.text):
                    # 保留 ':' 由调用方消费
                    return parse_scalar(self.text[start : self.pos].strip())
            if ch in ",}]":
                break
            self.pos += 1
        raise YamlLiteError("flow 映射缺少 ':'", self.line)

    def _quoted(self, quote: str) -> str:
        text = self.text
        self.pos += 1
        out: List[str] = []
        while self.pos < len(text):
            ch = text[self.pos]
            if quote == "'":
                if ch == "'":
                    if self.pos + 1 < len(text) and text[self.pos + 1] == "'":
                        out.append("'")
                        self.pos += 2
                        continue
                    self.pos += 1
                    return "".join(out)
                out.append(ch)
                self.pos += 1
                continue
            if ch == "\\":
                decoded, new_pos = _decode_escape(text, self.pos + 1, self.line)
                out.append(decoded)
                self.pos = new_pos
                continue
            if ch == '"':
                self.pos += 1
                return "".join(out)
            out.append(ch)
            self.pos += 1
        raise YamlLiteError("引号未闭合", self.line)

    def _plain(self) -> str:
        text = self.text
        start = self.pos
        while self.pos < len(text):
            ch = text[self.pos]
            if ch in ",]}":
                break
            if ch == "#" and self.pos > start and text[self.pos - 1] in " \t":
                break
            self.pos += 1
        return text[start : self.pos].strip()


# ---------------------------------------------------------------------------
# 块结构解析
# ---------------------------------------------------------------------------

_KEYWORD_CLASS = re.compile(r"\b(class|interface|enum)\b")


class _LiteParser:
    def __init__(self, lines: List[str]) -> None:
        self.lines: List[str] = list(lines)
        self.count = len(self.lines)
        self.index = 0

    # -- 基础工具
    def _peek(self) -> Optional[Tuple[int, int, str]]:
        """返回下一个有效行的 (行下标, 缩进, 去掉缩进的内容)，不移动游标。"""
        i = self.index
        while i < self.count:
            raw = self.lines[i]
            body = raw.strip()
            if body == "" or body.startswith("#"):
                i += 1
                continue
            indent = len(raw) - len(raw.lstrip(" "))
            leading = raw[: len(raw) - len(raw.lstrip(" \t"))]
            if "\t" in leading:
                raise YamlLiteError("缩进不能使用制表符", i + 1)
            return i, indent, raw[indent:]
        return None

    @staticmethod
    def _looks_like_mapping_entry(text: str) -> bool:
        if not text or text.startswith("#"):
            return False
        if text[0] in "[{":
            return False
        if text[0] in "\"'":
            end = _scan_quoted_end(text, 0, text[0])
            if end is None:
                return False
            rest = text[end:].lstrip()
            return rest.startswith(":")
        depth = 0
        for index, ch in enumerate(text):
            if ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
            elif ch == ":" and depth == 0:
                nxt = text[index + 1] if index + 1 < len(text) else ""
                if nxt == "" or nxt in " \t":
                    return bool(text[:index].strip())
        return False

    # -- 入口
    def parse(self) -> Any:
        self._skip_preamble()
        head = self._peek()
        if head is None:
            return None
        if head[2] in ("---", "..."):
            self.index = head[0] + 1
            head = self._peek()
            if head is None:
                return None
        return self._parse_block(head[1])

    def _skip_preamble(self) -> None:
        head = self._peek()
        while head is not None and (head[2].startswith("%") or head[2] in ("---", "...")):
            self.index = head[0] + 1
            head = self._peek()

    # -- 块
    def _parse_block(self, indent: int) -> Any:
        head = self._peek()
        if head is None:
            return None
        _, line_indent, text = head
        if line_indent < indent:
            return None
        if text.startswith("?"):
            raise YamlLiteError("不支持复杂键 '?'", head[0] + 1)
        if text == "-" or text.startswith("- "):
            return self._parse_sequence(line_indent)
        if self._looks_like_mapping_entry(text):
            return self._parse_mapping(line_indent)
        index = head[0]
        self.index = index + 1
        return self._parse_inline_value(text, line_indent, index)

    def _parse_mapping(self, indent: int) -> dict:
        result: dict = {}
        while True:
            head = self._peek()
            if head is None:
                break
            index, line_indent, text = head
            if line_indent < indent:
                break
            if line_indent > indent:
                raise YamlLiteError("缩进错误，期望 %d 个空格" % indent, index + 1)
            if text == "-" or text.startswith("- "):
                break
            if text in ("---", "..."):
                break
            split = self._split_key(text, index)
            if split is None:
                raise YamlLiteError("无法解析的映射项: %s" % text, index + 1)
            key, rest = split
            if key == "<<":
                raise YamlLiteError("不支持合并键 '<<'", index + 1)
            self.index = index + 1
            result[key] = self._parse_value_after_key(rest, line_indent, index)
        return result

    def _parse_sequence(self, indent: int) -> List[Any]:
        items: List[Any] = []
        while True:
            head = self._peek()
            if head is None:
                break
            index, line_indent, text = head
            if line_indent != indent:
                break
            if not (text == "-" or text.startswith("- ")):
                break
            rest = text[1:]
            after = rest.lstrip(" ")
            offset = indent + 1 + (len(rest) - len(after))
            if after == "" or after.startswith("#"):
                self.index = index + 1
                nested = self._peek()
                if nested is None or nested[1] <= indent:
                    items.append(None)
                else:
                    items.append(self._parse_block(nested[1]))
                continue
            if after == "-" or after.startswith("- ") or self._looks_like_mapping_entry(after):
                # 把 "- " 替换为空格，让子块按普通块解析
                self.lines[index] = " " * offset + after
                if after == "-" or after.startswith("- "):
                    items.append(self._parse_sequence(offset))
                else:
                    items.append(self._parse_mapping(offset))
                continue
            self.index = index + 1
            items.append(self._parse_inline_value(after, indent, index))
        return items

    def _parse_value_after_key(self, rest: str, indent: int, index: int) -> Any:
        rest = rest.strip()
        if rest == "" or rest.startswith("#"):
            head = self._peek()
            if head is None:
                return None
            _, next_indent, next_text = head
            if next_text == "-" or next_text.startswith("- "):
                if next_indent >= indent:
                    return self._parse_sequence(next_indent)
                return None
            if next_indent > indent:
                return self._parse_block(next_indent)
            return None
        return self._parse_inline_value(rest, indent, index)

    # -- 键
    def _split_key(self, text: str, index: int) -> Optional[Tuple[Any, str]]:
        if text.startswith("?"):
            raise YamlLiteError("不支持复杂键 '?'", index + 1)
        if text[0] in "\"'":
            found = _scan_quoted_span(text[1:], text[0], index + 1)
            if found is None:
                raise YamlLiteError("引号键必须在同一行内闭合", index + 1)
            key_text, rest = found
            pos = 0
            while pos < len(rest) and rest[pos] in " \t":
                pos += 1
            if pos >= len(rest) or rest[pos] != ":":
                return None
            return key_text, rest[pos + 1 :]
        depth = 0
        for pos, ch in enumerate(text):
            if ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
            elif ch == "#" and pos > 0 and text[pos - 1] in " \t":
                break
            elif ch == ":" and depth == 0:
                nxt = text[pos + 1] if pos + 1 < len(text) else ""
                if nxt == "" or nxt in " \t":
                    key_text = text[:pos].strip()
                    if not key_text:
                        return None
                    return parse_scalar(key_text), text[pos + 1 :]
        return None

    # -- 值
    def _parse_inline_value(self, text: str, indent: int, index: int) -> Any:
        text = text.strip()
        if not text:
            return None
        ch = text[0]
        if ch in "|>":
            return self._parse_block_scalar(text, indent, index)
        if ch in "[{":
            return self._parse_flow(text, index)
        if ch in "\"'":
            return self._read_quoted(text, index)[0]
        if ch in "&*":
            raise YamlLiteError("不支持锚点/别名（%s）" % ch, index + 1)
        if ch == "!":
            raise YamlLiteError("不支持 YAML 标签（!!）", index + 1)
        return self._parse_plain(text, indent, index)

    def _parse_flow(self, text: str, index: int) -> Any:
        buffer = text
        line = index
        while not _flow_balanced(buffer):
            line += 1
            if line >= self.count:
                raise YamlLiteError("flow 集合未闭合", index + 1)
            buffer += " " + self.lines[line].strip()
        self.index = line + 1
        return _FlowParser(buffer, index + 1).parse()

    def _parse_plain(self, text: str, indent: int, index: int) -> Any:
        parts: List[Optional[str]] = [_strip_comment(text)]
        line = index + 1
        while line < self.count:
            raw = self.lines[line]
            body = raw.strip()
            if body == "":
                probe = line
                while probe < self.count and self.lines[probe].strip() == "":
                    probe += 1
                if probe >= self.count:
                    break
                next_raw = self.lines[probe]
                next_indent = len(next_raw) - len(next_raw.lstrip(" "))
                next_body = next_raw.strip()
                if next_indent <= indent or next_body.startswith("#"):
                    break
                if next_body.startswith("- ") or self._looks_like_mapping_entry(next_body):
                    break
                parts.append(None)
                line = probe
                continue
            line_indent = len(raw) - len(raw.lstrip(" "))
            if line_indent <= indent:
                break
            if body.startswith("#") or body.startswith("- "):
                break
            if self._looks_like_mapping_entry(body):
                break
            parts.append(_strip_comment(body))
            line += 1
        if len(parts) > 1:
            self.index = line
            return parse_scalar(_fold_lines(parts))
        return parse_scalar(parts[0] or "")

    def _read_quoted(self, text: str, index: int) -> Tuple[str, int]:
        """读取可能跨行的引号标量，返回 (值, 结束行下标)。"""
        quote = text[0]
        segments: List[Optional[str]] = []
        buffer = text[1:]
        line = index
        while True:
            found = _scan_quoted_span(buffer, quote, line + 1)
            if found is not None:
                value, rest = found
                tail = rest.strip()
                if tail and not tail.startswith("#"):
                    raise YamlLiteError("引号标量之后存在多余内容: %s" % tail, line + 1)
                segments.append(value)
                self.index = line + 1
                return _fold_lines(segments), line
            segments.append(buffer)
            line += 1
            while line < self.count and self.lines[line].strip() == "":
                segments.append(None)
                line += 1
            if line >= self.count:
                raise YamlLiteError("引号标量未闭合", index + 1)
            buffer = self.lines[line].strip()

    def _parse_block_scalar(self, header: str, indent: int, index: int) -> str:
        style = header[0]
        rest = header[1:].strip()
        chomp = ""
        explicit: Optional[int] = None
        for ch in rest:
            if ch in "+-":
                chomp = ch
            elif ch.isdigit():
                explicit = int(ch)
            elif ch in " \t":
                continue
            elif ch == "#":
                break
            else:
                raise YamlLiteError("无法识别的块标量指示符: %s" % header, index + 1)
        lines: List[Optional[str]] = []
        blanks = 0
        trailing = 0
        content_indent = (indent + explicit) if explicit else None
        line = index + 1
        while line < self.count:
            raw = self.lines[line]
            if raw.strip() == "":
                lines.append(None)
                line += 1
                continue
            line_indent = len(raw) - len(raw.lstrip(" "))
            if content_indent is None:
                if line_indent <= indent:
                    break
                content_indent = line_indent
            if line_indent < content_indent:
                break
            lines.append(raw[content_indent:])
            line += 1
        self.index = line
        while lines and lines[-1] is None:
            lines.pop()
            trailing += 1
        if style == "|":
            body = "\n".join("" if item is None else item for item in lines)
        else:
            body = _fold_lines(lines)
        if chomp == "-":
            return body
        if chomp == "+":
            if not body:
                return "\n" * trailing
            return body + "\n" * (1 + trailing)
        return body + "\n" if body else ""


# ---------------------------------------------------------------------------
# 扫描辅助
# ---------------------------------------------------------------------------


def _scan_quoted_end(text: str, start: int, quote: str) -> Optional[int]:
    """返回引号结束后的下标（不含引号后的内容）；未闭合返回 None。"""
    pos = start + 1
    while pos < len(text):
        ch = text[pos]
        if quote == "'":
            if ch == "'":
                if pos + 1 < len(text) and text[pos + 1] == "'":
                    pos += 2
                    continue
                return pos + 1
            pos += 1
            continue
        if ch == "\\":
            pos += 2
            continue
        if ch == '"':
            return pos + 1
        pos += 1
    return None


def _scan_quoted_span(text: str, quote: str, line: int) -> Optional[Tuple[str, str]]:
    """在单行文本中扫描引号标量；未闭合返回 None，否则返回 (值, 剩余文本)。"""
    pos = 0
    out: List[str] = []
    while pos < len(text):
        ch = text[pos]
        if quote == "'":
            if ch == "'":
                if pos + 1 < len(text) and text[pos + 1] == "'":
                    out.append("'")
                    pos += 2
                    continue
                return "".join(out), text[pos + 1 :]
            out.append(ch)
            pos += 1
            continue
        if ch == "\\":
            decoded, new_pos = _decode_escape(text, pos + 1, line)
            out.append(decoded)
            pos = new_pos
            continue
        if ch == '"':
            return "".join(out), text[pos + 1 :]
        out.append(ch)
        pos += 1
    return None


def _flow_balanced(text: str) -> bool:
    """流式集合的括号是否已闭合（忽略引号内的括号）。"""
    depth = 0
    pos = 0
    while pos < len(text):
        ch = text[pos]
        if ch in "\"'":
            end = _scan_quoted_end(text, pos, ch)
            if end is None:
                return False
            pos = end
            continue
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth < 0:
                return True
        pos += 1
    return depth <= 0


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------


def load_yaml_lite(text: str) -> Any:
    """始终使用内置解析器解析 YAML 文本。"""
    if not isinstance(text, str):
        raise YamlLiteError("YAML 内容必须是字符串")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if normalized.startswith("\ufeff"):
        normalized = normalized[1:]
    return _LiteParser(normalized.split("\n")).parse()


def _pyyaml_loader() -> Any:
    """构造关闭 timestamp 隐式解析的 SafeLoader，保证日期标量仍是字符串。"""
    class _Loader(_pyyaml.SafeLoader):  # type: ignore[misc]
        pass

    resolvers = {}
    for first_char, mappings in _Loader.yaml_implicit_resolvers.items():
        kept = [item for item in mappings if item[0] != "tag:yaml.org,2002:timestamp"]
        if kept:
            resolvers[first_char] = kept
    _Loader.yaml_implicit_resolvers = resolvers
    return _Loader


def load_yaml(text: str) -> Any:
    """解析 YAML 文本：有 PyYAML 时优先使用，否则使用内置解析器。"""
    if not isinstance(text, str):
        raise YamlLiteError("YAML 内容必须是字符串")
    if not HAS_PYYAML:
        return load_yaml_lite(text)
    try:
        loader = _pyyaml_loader()
    except Exception:  # pragma: no cover - PyYAML 版本差异
        loader = _pyyaml.SafeLoader
    try:
        return _pyyaml.load(text, Loader=loader)
    except Exception as exc:
        try:
            return load_yaml_lite(text)
        except YamlLiteError:
            raise _wrap_pyyaml_error(exc)


def _wrap_pyyaml_error(exc: Exception) -> YamlLiteError:
    line = 0
    mark = getattr(exc, "problem_mark", None)
    if mark is not None:
        line = int(getattr(mark, "line", 0) or 0) + 1
    message = str(getattr(exc, "problem", None) or exc)
    return YamlLiteError(message, line)


def load_file(path: Any) -> Any:
    """读取并解析 YAML 文件（utf-8，容忍 BOM）。"""
    target = Path(path)
    text = target.read_text(encoding="utf-8-sig")
    return load_yaml(text)
