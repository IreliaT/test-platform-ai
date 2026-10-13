"""JSONPath 子集实现（仅标准库，无第三方依赖）。

模块职责
--------
为断言（``assertions.py``）与变量提取（``variables.py``）提供统一的 JSON 取值能力。

支持的语法子集（与 ``docs/architecture/INTERFACES.md`` 1.1 一致）：

===============  ==========================================================
语法              说明
===============  ==========================================================
``$``            根节点
``.name``        子字段
``['name']``    子字段（单引号，支持空格等特殊字符）
``["name"]``    子字段（双引号）
``[0]``         数组下标
``[-1]``        负下标（从末尾倒数）
``[*]`` / ``.*`` 数组元素 / 对象值全展开
``..name``       递归下降查找字段（深度优先，包含列表内部）
``[1:3]``        数组切片，支持 ``[1:]`` ``[:2]`` ``[::2]`` ``[::-1]``
``[?(@.a==1)]``  过滤：``== != > >= < <= =~``、``&&``、``||``、括号
===============  ==========================================================

设计约定
--------
- 结果始终为列表；**路径无匹配返回空列表而不是抛异常**，只有语法错误才抛
  :class:`JsonPathError`。
- 省略 ``$`` 的表达式按 ``$.<expr>`` 处理；``[0].a`` 这类以 ``[`` 开头的表达式按 ``$[0].a``
  处理；空表达式等价于 ``$``（取整个文档），便于提取器在表达式留空时取整个响应体。
- ``..name`` 按文档顺序做深度优先遍历，包含列表元素内部的字段（如 ``$..id`` 能命中
  ``{"items": [{"id": 1}]}`` 中的 ``id``）。
- 过滤器中裸路径条件（``[?(@.b)]``）表示「字段存在」（值为 ``null`` 也算存在），
  因此 ``$.a[?(@.b)].c`` 是存在性过滤后取子字段。
- 大小比较仅在两侧都是数字时按数值比较，否则按字符串比较。
- 未提供 ``set_value``：同一路径可能命中多个位置（递归下降、``[*]``、过滤器、切片），
  写入语义无法简单确定，按接口契约只交付只读能力。
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, List, Optional, Sequence, Tuple

__all__ = ["JsonPathError", "query", "first", "exists"]

_WS = " \t\r\n"
# 点号字段名的终止字符：这些字符属于路径/过滤器语法，不能出现在 .name 里
_DOT_NAME_STOP = set(".[]()\"'?,=!<>&|") | set(_WS)
# true/false/null 关键字后面允许出现的字符
_KEYWORD_END = set(_WS) | set(")]}&|,=!<>")


class JsonPathError(ValueError):
    """JSONPath 表达式语法错误。"""


class _Missing:
    """内部哨兵：路径在当前节点上不存在。"""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试
        return "<MISSING>"


_MISSING = _Missing()


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


class _Parser:
    """把 JSONPath 表达式解析为步骤序列（递归下降 + 过滤器表达式）。"""

    def __init__(self, text: str, original: str = "") -> None:
        self.text = text
        self.original = original or text
        self.pos = 0
        self.length = len(text)

    # -- 基础工具 ---------------------------------------------------------
    def _fail(self, message: str) -> None:
        fragment = self.text[self.pos:self.pos + 12] or "<表达式结束>"
        raise JsonPathError(
            'JSONPath 语法错误：%s（位置 %d，片段 "%s"，表达式 "%s"）'
            % (message, self.pos, fragment, self.original)
        )

    def _eof(self) -> bool:
        return self.pos >= self.length

    def _peek(self, offset: int = 0) -> str:
        index = self.pos + offset
        return self.text[index] if index < self.length else ""

    def _skip_ws(self) -> None:
        while self.pos < self.length and self.text[self.pos] in _WS:
            self.pos += 1

    def _expect(self, char: str) -> None:
        if self._peek() != char:
            self._fail('期望 "%s"' % char)
        self.pos += 1

    # -- 路径 -------------------------------------------------------------
    def parse(self) -> List[Tuple[Any, ...]]:
        self._skip_ws()
        if self._peek() == "$":
            self.pos += 1
        steps: List[Tuple[Any, ...]] = []
        while True:
            self._skip_ws()
            if self._eof():
                break
            char = self._peek()
            if char == ".":
                steps.append(self._parse_dot())
            elif char == "[":
                steps.append(self._parse_bracket())
            else:
                self._fail('无法识别的片段（应以 "." 或 "[" 继续）')
        return steps

    def _parse_dot(self) -> Tuple[Any, ...]:
        if self.text.startswith("..", self.pos):
            self.pos += 2
            self._skip_ws()
            if self._peek() == "*":
                self.pos += 1
                return ("recursive_wildcard",)
            return ("recursive", self._read_dot_name())
        self.pos += 1
        self._skip_ws()
        if self._peek() == "*":
            self.pos += 1
            return ("wildcard",)
        if self._peek() == "[":
            self._fail('"." 后缺少字段名')
        return ("child", self._read_dot_name())

    def _read_dot_name(self) -> str:
        start = self.pos
        while self.pos < self.length and self.text[self.pos] not in _DOT_NAME_STOP:
            self.pos += 1
        name = self.text[start:self.pos]
        if not name:
            self._fail("缺少字段名")
        return name

    def _read_quoted(self) -> str:
        quote = self._peek()
        if quote not in ("'", '"'):
            self._fail("缺少引号")
        self.pos += 1
        chars: List[str] = []
        while True:
            if self._eof():
                self._fail("字符串未闭合")
            char = self.text[self.pos]
            if char == "\\" and self.pos + 1 < self.length:
                chars.append(self.text[self.pos + 1])
                self.pos += 2
                continue
            if char == quote:
                self.pos += 1
                return "".join(chars)
            chars.append(char)
            self.pos += 1

    def _read_until_bracket(self) -> str:
        """读取到 ``]`` 为止（消费 ``]``），返回中间的原文。"""
        start = self.pos
        while self.pos < self.length and self.text[self.pos] != "]":
            self.pos += 1
        if self._eof():
            self._fail('"[" 未闭合')
        raw = self.text[start:self.pos]
        self.pos += 1
        return raw

    def _parse_bracket(self) -> Tuple[Any, ...]:
        self.pos += 1  # 消费 '['
        self._skip_ws()
        char = self._peek()
        if char == "":
            self._fail('"[" 未闭合')
        if char == "?":
            self.pos += 1
            self._skip_ws()
            if self._peek() != "(":
                self._fail('过滤器必须以 "[?(" 开头')
            self.pos += 1
            node = self._parse_or()
            self._skip_ws()
            self._expect(")")
            self._skip_ws()
            self._expect("]")
            return ("filter", node)
        if char in ("'", '"'):
            name = self._read_quoted()
            self._skip_ws()
            self._expect("]")
            return ("child", name)
        if char == "*":
            self.pos += 1
            self._skip_ws()
            self._expect("]")
            return ("wildcard",)
        raw = self._read_until_bracket().strip()
        if ":" in raw:
            return ("slice",) + self._parse_slice(raw)
        try:
            index = int(raw)
        except ValueError:
            self._fail('无法解析的下标 "%s"（字符串字段名请使用引号）' % raw)
        return ("index", index)

    def _parse_slice(self, raw: str) -> Tuple[Optional[int], Optional[int], Optional[int]]:
        parts = raw.split(":")
        if len(parts) not in (2, 3):
            self._fail('切片格式错误 "%s"' % raw)
        values: List[Optional[int]] = []
        for part in parts:
            part = part.strip()
            if part == "":
                values.append(None)
                continue
            try:
                values.append(int(part))
            except ValueError:
                self._fail('切片参数必须是整数 "%s"' % part)
        while len(values) < 3:
            values.append(None)
        if values[2] == 0:
            self._fail("切片步长不能为 0")
        return values[0], values[1], values[2]

    # -- 过滤器表达式 -----------------------------------------------------
    def _parse_or(self) -> Tuple[Any, ...]:
        node = self._parse_and()
        while True:
            self._skip_ws()
            if self.text.startswith("||", self.pos):
                self.pos += 2
                node = ("or", node, self._parse_and())
            else:
                return node

    def _parse_and(self) -> Tuple[Any, ...]:
        node = self._parse_unary()
        while True:
            self._skip_ws()
            if self.text.startswith("&&", self.pos):
                self.pos += 2
                node = ("and", node, self._parse_unary())
            else:
                return node

    def _parse_unary(self) -> Tuple[Any, ...]:
        self._skip_ws()
        char = self._peek()
        if char == "(":
            self.pos += 1
            node = self._parse_or()
            self._skip_ws()
            self._expect(")")
            return node
        if char == "!":
            self.pos += 1
            return ("not", self._parse_unary())
        return self._parse_comparison()

    def _parse_comparison(self) -> Tuple[Any, ...]:
        left = self._parse_operand()
        self._skip_ws()
        operator = self._read_comparison_operator()
        if operator is None:
            return ("test", left)
        right = self._parse_operand()
        return ("cmp", operator, left, right)

    def _read_comparison_operator(self) -> Optional[str]:
        for operator in ("==", "!=", ">=", "<=", "=~", ">", "<"):
            if self.text.startswith(operator, self.pos):
                self.pos += len(operator)
                return operator
        return None

    def _parse_operand(self) -> Tuple[Any, ...]:
        self._skip_ws()
        char = self._peek()
        if char == "":
            self._fail("过滤器表达式不完整")
        if char == "@":
            return self._parse_at_path()
        if char in ("'", '"'):
            return ("lit", self._read_quoted())
        if char.isdigit() or char in "+-":
            return ("lit", self._read_number())
        lowered = self.text[self.pos:self.pos + 5].lower()
        for keyword, value in (("true", True), ("false", False), ("null", None)):
            if lowered.startswith(keyword):
                end = self.pos + len(keyword)
                if self.text[end:end + 1] in _KEYWORD_END:
                    self.pos = end
                    return ("lit", value)
        self._fail("过滤器只支持 @ 路径、数字、字符串与 true/false/null 字面量")

    def _read_number(self) -> Any:
        match = re.match(r"[+-]?\d+(\.\d+)?([eE][+-]?\d+)?", self.text[self.pos:])
        if not match:
            self._fail("数字字面量格式错误")
        raw = match.group(0)
        self.pos += len(raw)
        if "." in raw or "e" in raw or "E" in raw:
            return float(raw)
        return int(raw)

    def _parse_at_path(self) -> Tuple[Any, ...]:
        self.pos += 1  # 消费 '@'
        parts: List[Tuple[Any, ...]] = []
        while True:
            char = self._peek()
            if char == ".":
                self.pos += 1
                self._skip_ws()
                if self._peek() == "*":
                    self._fail("过滤器路径不支持通配符")
                parts.append(("child", self._read_dot_name()))
                continue
            if char == "[":
                self.pos += 1
                self._skip_ws()
                if self._peek() in ("'", '"'):
                    name = self._read_quoted()
                    self._skip_ws()
                    self._expect("]")
                    parts.append(("child", name))
                    continue
                raw = self._read_until_bracket().strip()
                try:
                    parts.append(("index", int(raw)))
                except ValueError:
                    self._fail('过滤器路径只支持 "[下标]" 或 "[\'字段名\']"')
                continue
            break
        return ("path", tuple(parts))


def _normalize(expression: str) -> str:
    """补全省略的 ``$``。"""
    text = expression.strip()
    if not text:
        return "$"
    if text.startswith("$"):
        return text
    if text.startswith(".") or text.startswith("["):
        return "$" + text
    return "$." + text


@lru_cache(maxsize=256)
def _compile(expression: str) -> Tuple[Tuple[Any, ...], ...]:
    parser = _Parser(_normalize(expression), expression)
    return tuple(parser.parse())


# ---------------------------------------------------------------------------
# 求值
# ---------------------------------------------------------------------------


def _deep_equal(left: Any, right: Any) -> bool:
    """过滤器用的等值判断：字典忽略键顺序，布尔与数字严格区分，数字按数值比较。"""
    if isinstance(left, bool) or isinstance(right, bool):
        return _bool_literal(left) is not None and _bool_literal(left) is _bool_literal(right)
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, dict) and isinstance(right, dict):
        if set(left.keys()) != set(right.keys()):
            return False
        return all(_deep_equal(left[key], right[key]) for key in left)
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        if len(left) != len(right):
            return False
        return all(_deep_equal(a, b) for a, b in zip(left, right))
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left) == float(right)
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    return False


def _bool_literal(value: Any) -> Optional[bool]:
    """把布尔或 "true"/"false" 字面量归一为 bool，其它返回 None。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text == "true":
            return True
        if text == "false":
            return False
    return None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _compare_values(left: Any, operator: str, right: Any) -> bool:
    if operator == "==":
        return _deep_equal(left, right)
    if operator == "!=":
        return not _deep_equal(left, right)
    if operator == "=~":
        if not isinstance(right, str):
            return False
        try:
            return re.search(right, _as_text(left)) is not None
        except re.error:
            return False
    left_num = _number(left)
    right_num = _number(right)
    if left_num is not None and right_num is not None:
        first: Any = left_num
        second: Any = right_num
    else:
        first = _as_text(left)
        second = _as_text(right)
    if operator == ">":
        return first > second
    if operator == ">=":
        return first >= second
    if operator == "<":
        return first < second
    if operator == "<=":
        return first <= second
    return False


def _exists(node: Tuple[Any, ...], item: Any) -> bool:
    return _eval_node(node, item) is not _MISSING


def _eval_node(node: Tuple[Any, ...], item: Any) -> Any:
    """求值过滤器 AST；路径不存在时返回 :data:`_MISSING`。"""
    kind = node[0]
    if kind == "lit":
        return node[1]
    if kind == "path":
        value = item
        for part in node[1]:
            if part[0] == "child":
                if isinstance(value, dict) and part[1] in value:
                    value = value[part[1]]
                else:
                    return _MISSING
            else:
                index = part[1]
                if isinstance(value, (list, tuple)) and -len(value) <= index < len(value):
                    value = value[index]
                else:
                    return _MISSING
        return value
    if kind == "test":
        return _truth(node[1], item)
    if kind == "not":
        return not _truth(node[1], item)
    if kind == "and":
        return _truth(node[1], item) and _truth(node[2], item)
    if kind == "or":
        return _truth(node[1], item) or _truth(node[2], item)
    if kind == "cmp":
        left = _eval_node(node[2], item)
        right = _eval_node(node[3], item)
        if left is _MISSING or right is _MISSING:
            return False
        return _compare_values(left, node[1], right)
    return False


def _truth(node: Tuple[Any, ...], item: Any) -> bool:
    """布尔上下文求值：裸路径按「存在」判断，其余按真假判断。"""
    if node[0] == "path":
        return _exists(node, item)
    if node[0] == "test":
        return _truth(node[1], item)
    value = _eval_node(node, item)
    if value is _MISSING:
        return False
    return bool(value)


def _recursive_collect(node: Any, name: str, out: List[Any]) -> None:
    if isinstance(node, dict):
        if name in node:
            out.append(node[name])
        for value in node.values():
            _recursive_collect(value, name, out)
    elif isinstance(node, (list, tuple)):
        for value in node:
            _recursive_collect(value, name, out)


def _recursive_collect_all(node: Any, out: List[Any]) -> None:
    if isinstance(node, dict):
        for value in node.values():
            out.append(value)
            _recursive_collect_all(value, out)
    elif isinstance(node, (list, tuple)):
        for value in node:
            out.append(value)
            _recursive_collect_all(value, out)


def _apply_step(nodes: Sequence[Any], step: Tuple[Any, ...]) -> List[Any]:
    kind = step[0]
    out: List[Any] = []
    if kind == "child":
        name = step[1]
        for node in nodes:
            if isinstance(node, dict) and name in node:
                out.append(node[name])
        return out
    if kind == "index":
        index = step[1]
        for node in nodes:
            if isinstance(node, (list, tuple)) and -len(node) <= index < len(node):
                out.append(node[index])
        return out
    if kind == "wildcard":
        for node in nodes:
            if isinstance(node, dict):
                out.extend(node.values())
            elif isinstance(node, (list, tuple)):
                out.extend(node)
        return out
    if kind == "recursive":
        for node in nodes:
            _recursive_collect(node, step[1], out)
        return out
    if kind == "recursive_wildcard":
        for node in nodes:
            _recursive_collect_all(node, out)
        return out
    if kind == "slice":
        _, start, stop, steplen = step
        for node in nodes:
            if isinstance(node, (list, tuple)):
                for index in range(*slice(start, stop, steplen).indices(len(node))):
                    out.append(node[index])
        return out
    if kind == "filter":
        predicate = step[1]
        for node in nodes:
            if isinstance(node, (list, tuple)):
                out.extend(item for item in node if _truth(predicate, item))
            elif isinstance(node, dict):
                out.extend(value for value in node.values() if _truth(predicate, value))
        return out
    return out


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------


def query(data: Any, expression: str) -> List[Any]:
    """按 JSONPath 表达式取值，返回所有匹配（无匹配返回空列表）。

    语法错误抛 :class:`JsonPathError`；路径无匹配不抛异常。
    """
    if not isinstance(expression, str):
        raise JsonPathError("JSONPath 表达式必须是字符串，实际为 %s" % type(expression).__name__)
    steps = _compile(expression)
    nodes: List[Any] = [data]
    for step in steps:
        if not nodes:
            return []
        nodes = _apply_step(nodes, step)
    return nodes


def first(data: Any, expression: str, default: Any = None) -> Any:
    """返回第一个匹配值，无匹配时返回 ``default``。"""
    results = query(data, expression)
    return results[0] if results else default


def exists(data: Any, expression: str) -> bool:
    """路径是否有匹配（命中 ``null`` 也算存在）；语法错误仍抛 :class:`JsonPathError`。"""
    return bool(query(data, expression))
