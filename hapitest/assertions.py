"""断言求值：把 :class:`~hapitest.models.AssertionSpec` 应用到一次 HTTP 响应上。

本模块被执行引擎（``runner.py``）、导入器（生成默认断言）与 Web API 共用，
公开函数签名与 ``docs/architecture/INTERFACES.md`` 1.2 完全一致。

取值行为
--------
- ``status_code`` 取 ``context.status``；``body_text`` 取响应体文本；
  ``json_path`` 按 :mod:`hapitest.jsonpath` 取值（``path`` 为空时按 ``$`` 处理并在 message 中提示）；
  ``header`` / ``cookie`` 取值**不区分大小写**，且忽略 ``path``（``name`` 为空时用 ``path`` 兜底，
  方便导入器把响应头名写进 `path`）；``response_time`` 取 ``context.duration_ms``；
  ``variable`` 取 ``context.variables[name]``；``json_schema`` 用 ``expected`` 作为 JSON Schema
  校验 ``path`` 处的取值（忽略 ``operator``）。
- 取值失败（路径无匹配、响应头不存在、变量不存在）产出 ``passed=False`` 并在 message 中说明原因；
  唯一例外是 ``not_exists``：路径无匹配时算通过。
- ``json_path`` / ``json_schema`` 需要 JSON 响应体：``body_json`` 为空时会尝试用 ``json.loads``
  解析 ``body_text``，解析失败产出 ``passed=False``，message 说明「响应体不是合法 JSON」；
  ``body_text`` 断言不受影响。

比较规则（``compare``）
----------------------
- 两侧都能转成浮点数时按数值比较（``"10"`` 与 ``10`` 相等，``"10" > 9`` 成立）；
  否则按字符串比较。因此 ``eq`` / ``ne`` 对 ``10`` 与 ``"10"`` 判定相等。
- 布尔值与 ``"true"`` / ``"false"``（忽略大小写）在 ``eq`` / ``ne`` 下互相等价；
  布尔值不与数字等价（``true`` 不等于 ``1``）。
- ``is_true`` / ``is_false`` 额外接受 ``1/0``、``yes/no``、``on/off``（忽略大小写）。
- ``approx`` 为相对容差比较，默认 ``1e-6``，可用 ``AssertionSpec.tolerance`` 覆盖；
  判定式为 ``|actual - expected| <= tolerance * max(1, |expected|)``（期望值接近 0 时有绝对下限）。
- ``json_contains`` 表示「期望值是实际值的递归子集」：字典按键递归包含，列表按元素包含匹配
  （标量等价于相等），标量按 JSON 语义相等比较。
- ``json_equals`` 表示 JSON 语义深相等：字典忽略键顺序，列表顺序敏感，``true`` 与 ``1`` 不相等。
- ``contains`` 对列表按元素、对字典按键、其它按字符串子串判断。
- ``regex`` 使用 ``re.search``；正则非法时返回失败并给出原因，不抛异常。
- ``exists`` / ``not_exists`` / ``is_empty`` / ``not_empty`` / ``is_true`` / ``is_false``
  忽略 ``expected``。

其它约定
--------
- ``evaluate`` 永不抛异常：内部异常统一转为 ``passed=False``，message 含异常信息。
- ``enabled=False`` 的断言返回 ``passed=True``，message 说明「已禁用，跳过校验」。
- 断言失败时，``AssertionSpec.message``（自定义备注）会追加到结果 message 末尾。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import jsonpath, jsonschema_lite
from .models import OPERATORS, AssertionResult, AssertionSpec, HttpResponse

__all__ = [
    "AssertionContext",
    "build_context",
    "evaluate",
    "evaluate_all",
    "compare",
    "default_assertions_for",
]

_DEFAULT_TOLERANCE = 1e-6
_TRUE_LITERALS = ("true", "1", "yes", "y", "on")
_FALSE_LITERALS = ("false", "0", "no", "n", "off")
_TYPE_NAMES = ("string", "number", "integer", "boolean", "object", "array", "null")
_COOKIE_ATTRIBUTES = (
    "path",
    "domain",
    "expires",
    "max-age",
    "secure",
    "httponly",
    "samesite",
    "version",
    "comment",
)


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------


def _fmt(value: Any, limit: int = 120) -> str:
    """把取值渲染成简短文本（字符串带引号，保证空串可读）。"""
    if isinstance(value, str):
        text = json.dumps(value, ensure_ascii=False)
    elif value is None:
        text = "null"
    elif isinstance(value, bool):
        text = "true" if value else "false"
    else:
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


def _join(parts: Sequence[str]) -> str:
    return "；".join([str(part) for part in parts if part])


def _to_float(value: Any) -> Optional[float]:
    """能安全转成有限浮点数时返回 float，否则 None（布尔不参与数值比较）。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    else:
        return None
    if number != number or number in (float("inf"), float("-inf")):  # NaN / Inf
        return None
    return number


def _bool_literal(value: Any) -> Optional[bool]:
    """布尔或 "true"/"false" 字面量归一为 bool。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text == "true":
            return True
        if text == "false":
            return False
    return None


def _truth(value: Any) -> Optional[bool]:
    """宽松真假判断，无法判定时返回 None。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_LITERALS:
            return True
        if text in _FALSE_LITERALS:
            return False
        return None
    if isinstance(value, (list, tuple, dict)):
        return len(value) > 0
    return None


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value == ""
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


def _length_of(value: Any) -> Optional[int]:
    if isinstance(value, (str, list, tuple, dict)):
        return len(value)
    return None


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, (list, tuple)):
        return "array"
    return type(value).__name__


def _matches_type(value: Any, type_name: str) -> bool:
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "object":
        return isinstance(value, dict)
    if type_name == "array":
        return isinstance(value, (list, tuple))
    if type_name == "null":
        return value is None
    return False


def _json_equal(left: Any, right: Any) -> bool:
    """JSON 语义深相等：字典忽略键顺序，列表顺序敏感，布尔与数字严格区分。"""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, dict) and isinstance(right, dict):
        if set(left.keys()) != set(right.keys()):
            return False
        return all(_json_equal(left[key], right[key]) for key in left)
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        if len(left) != len(right):
            return False
        return all(_json_equal(a, b) for a, b in zip(left, right))
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left) == float(right)
    return bool(left == right)


def _json_contains(actual: Any, expected: Any) -> bool:
    """期望值是否为实际值的递归子集。"""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        return all(key in actual and _json_contains(actual[key], value) for key, value in expected.items())
    if isinstance(expected, (list, tuple)):
        if not isinstance(actual, (list, tuple)):
            return False
        return all(any(_json_contains(item, wanted) for item in actual) for wanted in expected)
    return _json_equal(actual, expected)


def _equal(actual: Any, expected: Any) -> bool:
    """eq/ne 使用的等值判断，遵循模块文档中的数值/布尔归一规则。"""
    if isinstance(actual, bool) or isinstance(expected, bool):
        left = _bool_literal(actual)
        right = _bool_literal(expected)
        return left is not None and left is right
    left_num = _to_float(actual)
    right_num = _to_float(expected)
    if left_num is not None and right_num is not None:
        return left_num == right_num
    if isinstance(actual, (dict, list, tuple)) or isinstance(expected, (dict, list, tuple)):
        return _json_equal(actual, expected)
    return str(actual) == str(expected)


def _pair_message(passed: bool, positive: str, negative: str, actual: Any, expected: Any) -> str:
    return "实际值 %s %s期望值 %s" % (_fmt(actual), positive if passed else negative, _fmt(expected))


def _lookup_ci(values: Any, name: str) -> Tuple[bool, Any, str]:
    """大小写不敏感查找；返回 (是否命中, 取值, 实际键名)。"""
    if not isinstance(values, Mapping) or not name:
        return False, None, ""
    if name in values:
        return True, values[name], name
    lowered = str(name).lower()
    for key, value in values.items():
        if str(key).lower() == lowered:
            return True, value, str(key)
    return False, None, ""


# ---------------------------------------------------------------------------
# 断言上下文
# ---------------------------------------------------------------------------


@dataclass
class AssertionContext:
    """断言求值所需的响应快照。``headers`` / ``cookies`` 查找不区分大小写。"""

    status: Optional[int] = None
    headers: Dict[str, str] = field(default_factory=dict)
    cookies: Dict[str, str] = field(default_factory=dict)
    body_text: str = ""
    body_json: Any = None
    duration_ms: int = 0
    url: str = ""
    variables: Dict[str, Any] = field(default_factory=dict)

    # -- 便捷取值（不区分大小写） ----------------------------------------
    def header(self, name: str, default: Any = None) -> Any:
        found, value, _key = _lookup_ci(self.headers, name)
        return value if found else default

    def cookie(self, name: str, default: Any = None) -> Any:
        found, value, _key = _lookup_ci(self.cookies, name)
        return value if found else default

    def variable(self, name: str, default: Any = None) -> Any:
        if isinstance(self.variables, Mapping) and name in self.variables:
            return self.variables[name]
        return default

    def has_json_body(self) -> bool:
        return self.body_json is not None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "headers": dict(self.headers),
            "cookies": dict(self.cookies),
            "body_text": self.body_text,
            "body_json": self.body_json,
            "duration_ms": self.duration_ms,
            "url": self.url,
            "variables": dict(self.variables),
        }


def _parse_cookie_pairs(value: str) -> Dict[str, str]:
    """解析 ``Set-Cookie`` / ``Cookie`` 头，跳过 Path/Expires 等属性。"""
    cookies: Dict[str, str] = {}
    if not value:
        return cookies
    try:
        jar = SimpleCookie()
        jar.load(value)
        for key, morsel in jar.items():
            cookies[str(key)] = str(morsel.value)
        if cookies:
            return cookies
    except Exception:  # 解析失败时退化为手工切分
        cookies = {}
    for chunk in str(value).split(";"):
        part = chunk.strip()
        if not part or "=" not in part:
            continue
        name, _sep, raw = part.partition("=")
        name = name.strip()
        if not name or name.lower() in _COOKIE_ATTRIBUTES:
            continue
        cookies[name] = raw.strip().strip('"')
    return cookies


def _collect_cookies(response: Any, headers: Mapping[str, str]) -> Dict[str, str]:
    """先取响应 ``Set-Cookie``，再回退到请求预览里的 ``Cookie`` 头（同名以响应为准）。"""
    cookies: Dict[str, str] = {}
    found, value, _key = _lookup_ci(headers, "Set-Cookie")
    if found:
        cookies.update(_parse_cookie_pairs(str(value)))
    preview = getattr(response, "request_preview", None)
    if isinstance(preview, Mapping):
        preview_headers = preview.get("headers")
        found, value, _key = _lookup_ci(preview_headers, "Cookie")
        if found:
            for key, item in _parse_cookie_pairs(str(value)).items():
                cookies.setdefault(key, item)
    return cookies


def build_context(response: HttpResponse, variables: Optional[Dict[str, Any]] = None) -> AssertionContext:
    """由 :class:`~hapitest.models.HttpResponse` 构造断言上下文。

    ``body_json`` 为空且 ``body_text`` 非空时会尝试解析 JSON（解析失败不抛异常，
    由 ``json_path`` / ``json_schema`` 断言给出「不是合法 JSON」的失败信息）。
    """
    if response is None:
        return AssertionContext(variables=dict(variables or {}))
    raw_headers = getattr(response, "headers", None) or {}
    headers: Dict[str, str] = {}
    if isinstance(raw_headers, Mapping):
        headers = {str(key): ("" if value is None else str(value)) for key, value in raw_headers.items()}
    body_text = getattr(response, "body_text", "") or ""
    if not isinstance(body_text, str):
        body_text = str(body_text)
    body_json = getattr(response, "body_json", None)
    parsed = body_json is not None
    if body_json is None and body_text.strip():
        try:
            body_json = json.loads(body_text)
            parsed = True
        except ValueError:
            parsed = False
    context = AssertionContext(
        status=getattr(response, "status", None),
        headers=headers,
        cookies=_collect_cookies(response, headers),
        body_text=body_text,
        body_json=body_json,
        duration_ms=int(getattr(response, "duration_ms", 0) or 0),
        url=str(getattr(response, "url", "") or ""),
        variables=dict(variables or {}),
    )
    context._body_parsed = parsed  # type: ignore[attr-defined]
    return context


# ---------------------------------------------------------------------------
# 取值
# ---------------------------------------------------------------------------


@dataclass
class _Target:
    """一次断言的取值结果。"""

    found: bool = True
    value: Any = None
    target: str = ""
    message: str = ""
    no_match: bool = False
    done: bool = False
    passed: bool = False


def _json_body(context: Any) -> Tuple[bool, Any, str]:
    """取 JSON 响应体；返回 (是否可用, 取值, 失败原因)。"""
    body_json = getattr(context, "body_json", None)
    body_text = getattr(context, "body_text", "") or ""
    if not isinstance(body_text, str):
        body_text = str(body_text)
    if body_json is not None:
        return True, body_json, ""
    if not body_text.strip():
        return False, None, "响应体为空，无法执行 JSON 断言"
    if getattr(context, "_body_parsed", None) is False:
        return False, None, "响应体不是合法 JSON，无法执行 JSON 断言"
    try:
        return True, json.loads(body_text), ""
    except ValueError:
        return False, None, "响应体不是合法 JSON，无法执行 JSON 断言"


def _target_text(spec: AssertionSpec) -> str:
    """断言目标的可读位置。"""
    a_type = str(spec.type or "").strip()
    if a_type in ("json_path", "json_schema"):
        return str(spec.path or "").strip() or "$"
    if a_type == "header":
        return "header:%s" % (spec.name or spec.path or "")
    if a_type == "cookie":
        return "cookie:%s" % (spec.name or spec.path or "")
    if a_type == "response_time":
        return "duration_ms"
    if a_type == "variable":
        return "variable:%s" % (spec.name or "")
    return a_type or "unknown"


def _extract_target(spec: AssertionSpec, context: Any) -> _Target:
    a_type = str(spec.type or "").strip()
    target = _target_text(spec)

    if a_type == "status_code":
        status = getattr(context, "status", None)
        if status is None:
            return _Target(False, target=target, message="响应中没有状态码（请求可能未完成）")
        return _Target(True, status, target)

    if a_type == "body_text":
        text = getattr(context, "body_text", "") or ""
        return _Target(True, "" if text is None else str(text), target)

    if a_type == "response_time":
        return _Target(True, int(getattr(context, "duration_ms", 0) or 0), target)

    if a_type == "json_path":
        path = str(spec.path or "").strip()
        notes: List[str] = []
        if not path:
            path = "$"
            notes.append("path 为空，已按 $ 处理")
        ok, body, reason = _json_body(context)
        if not ok:
            return _Target(False, target=path, message=_join([reason + ": " + path] + notes))
        try:
            results = jsonpath.query(body, path)
        except jsonpath.JsonPathError as exc:
            return _Target(False, target=path, message=_join(["JSONPath 表达式无效: %s" % exc] + notes))
        if not results:
            return _Target(False, target=path, message=_join(["路径无匹配: %s" % path] + notes), no_match=True)
        if len(results) > 1:
            notes.append("路径 %s 命中 %d 个结果，取第一个" % (path, len(results)))
        return _Target(True, results[0], path, _join(notes))

    if a_type == "json_schema":
        path = str(spec.path or "").strip() or "$"
        schema = spec.expected
        if schema is None:
            return _Target(False, target=path, message="json_schema 断言缺少 expected（JSON Schema 定义）")
        if not isinstance(schema, (dict, bool)):
            return _Target(False, target=path, message="JSON Schema 必须是对象或布尔值，实际为 %s" % type(schema).__name__)
        ok, body, reason = _json_body(context)
        if not ok:
            return _Target(False, target=path, message="%s: %s" % (reason, path))
        try:
            results = jsonpath.query(body, path)
        except jsonpath.JsonPathError as exc:
            return _Target(False, target=path, message="JSONPath 表达式无效: %s" % exc)
        if not results:
            return _Target(False, target=path, message="路径无匹配: %s" % path, no_match=True)
        instance = results[0]
        errors = jsonschema_lite.validate(instance, schema, path)
        if errors:
            text = "；".join(errors[:5])
            if len(errors) > 5:
                text += "；等共 %d 条错误" % len(errors)
            return _Target(False, value=instance, target=path, message="JSON Schema 校验失败：%s" % text, done=True)
        return _Target(True, instance, path, "JSON Schema 校验通过", done=True, passed=True)

    if a_type in ("header", "cookie"):
        name = str(spec.name or "").strip() or str(spec.path or "").strip()
        label = "header" if a_type == "header" else "cookie"
        if not name:
            return _Target(False, target=label, message="%s 断言缺少 name（%s 名称）" % (label, label))
        values = getattr(context, "headers" if a_type == "header" else "cookies", None) or {}
        found, value, _key = _lookup_ci(values, name)
        target = "%s:%s" % (label, name)
        if not found:
            return _Target(
                False,
                target=target,
                message="响应头不存在: %s" % name if a_type == "header" else "响应 Cookie 不存在: %s" % name,
                no_match=True,
            )
        return _Target(True, value, target)

    if a_type == "variable":
        name = str(spec.name or "").strip()
        if not name:
            return _Target(False, target="variable", message="variable 断言缺少 name（变量名）")
        variables = getattr(context, "variables", None)
        target = "variable:%s" % name
        if not isinstance(variables, Mapping):
            return _Target(False, target=target, message="上下文中不存在变量 %s" % name, no_match=True)
        if name not in variables:
            return _Target(False, target=target, message="上下文中不存在变量 %s" % name, no_match=True)
        return _Target(True, variables[name], target)

    return _Target(False, target=target, message="未知断言类型: %s" % (spec.type,))


# ---------------------------------------------------------------------------
# 比较
# ---------------------------------------------------------------------------


def _compare_order(operator: str, actual: Any, expected: Any) -> Tuple[bool, str]:
    words = {
        "gt": ("大于", "不大于"),
        "gte": ("大于等于", "小于"),
        "lt": ("小于", "不小于"),
        "lte": ("小于等于", "大于"),
    }
    positive, negative = words[operator]
    if actual is None or expected is None:
        return False, "无法比较：实际值 %s 与期望值 %s 中存在 null" % (_fmt(actual), _fmt(expected))
    left_num = _to_float(actual)
    right_num = _to_float(expected)
    if left_num is not None and right_num is not None:
        left: Any = left_num
        right: Any = right_num
    elif isinstance(actual, (dict, list, tuple)) or isinstance(expected, (dict, list, tuple)):
        return False, "操作符 %s 不支持对象/数组比较（实际值 %s）" % (operator, _fmt(actual))
    else:
        left = str(actual)
        right = str(expected)
    if operator == "gt":
        passed = left > right
    elif operator == "gte":
        passed = left >= right
    elif operator == "lt":
        passed = left < right
    else:
        passed = left <= right
    return passed, _pair_message(passed, positive, negative, actual, expected)


def _compare_contains(actual: Any, expected: Any, negate: bool) -> Tuple[bool, str]:
    if actual is None:
        return False, "实际值为 null，无法判断包含关系"
    if isinstance(actual, (list, tuple)):
        hit = any(_json_equal(item, expected) for item in actual)
        scope = "元素"
    elif isinstance(actual, dict):
        hit = any(_json_equal(key, expected) for key in actual)
        scope = "键"
    else:
        hit = str(expected) in str(actual)
        scope = "子串"
    passed = (not hit) if negate else hit
    positive = "不包含" if negate else "包含"
    negative = "包含" if negate else "不包含"
    message = "实际值 %s %s期望值 %s（按%s判断）" % (_fmt(actual), positive if passed else negative, _fmt(expected), scope)
    return passed, message


def _compare_length(operator: str, actual: Any, expected: Any) -> Tuple[bool, str]:
    length = _length_of(actual)
    if length is None:
        return False, "操作符 %s 仅支持字符串/数组/对象，实际值 %s 的类型为 %s" % (operator, _fmt(actual), _type_name(actual))
    wanted = _to_float(expected)
    if wanted is None:
        return False, "操作符 %s 的期望值必须是数字，实际为 %s" % (operator, _fmt(expected))
    if operator == "length_eq":
        passed = float(length) == wanted
        words = ("等于", "不等于")
    elif operator == "length_gt":
        passed = float(length) > wanted
        words = ("大于", "不大于")
    else:
        passed = float(length) < wanted
        words = ("小于", "不小于")
    message = "实际值 %s 的长度 %d %s期望长度 %s" % (_fmt(actual), length, words[0] if passed else words[1], _fmt(expected))
    return passed, message


def _compare_between(actual: Any, expected: Any) -> Tuple[bool, str]:
    if not isinstance(expected, (list, tuple)) or len(expected) != 2:
        return False, "操作符 between 的期望值必须是 [min, max]，实际为 %s" % _fmt(expected)
    low, high = expected[0], expected[1]
    actual_num = _to_float(actual)
    low_num = _to_float(low)
    high_num = _to_float(high)
    if actual_num is not None and low_num is not None and high_num is not None:
        passed = low_num <= actual_num <= high_num
    elif isinstance(actual, str) and isinstance(low, str) and isinstance(high, str):
        passed = low <= actual <= high
    else:
        return False, "操作符 between 需要可比较的数值或字符串，实际为 %s" % _fmt(actual)
    message = "实际值 %s %s区间 [%s, %s] 内" % (_fmt(actual), "落在" if passed else "不在", _fmt(low), _fmt(high))
    return passed, message


def _compare_approx(actual: Any, expected: Any, tolerance: Any) -> Tuple[bool, str]:
    limit_ratio = _DEFAULT_TOLERANCE
    if isinstance(tolerance, (int, float)) and not isinstance(tolerance, bool):
        limit_ratio = abs(float(tolerance))
    actual_num = _to_float(actual)
    expected_num = _to_float(expected)
    if actual_num is None or expected_num is None:
        return False, "操作符 approx 需要数值，实际值 %s / 期望值 %s" % (_fmt(actual), _fmt(expected))
    limit = limit_ratio * max(1.0, abs(expected_num))
    diff = abs(actual_num - expected_num)
    passed = diff <= limit
    if passed:
        return True, "实际值 %s 约等于期望值 %s（相对容差 %s）" % (_fmt(actual), _fmt(expected), _fmt(limit_ratio))
    return False, "实际值 %s 与期望值 %s 相差 %s，超出容差 %s（相对容差 %s）" % (
        _fmt(actual),
        _fmt(expected),
        _fmt(diff),
        _fmt(limit),
        _fmt(limit_ratio),
    )


def compare(actual: Any, operator: str, expected: Any, tolerance: Optional[float] = None) -> Tuple[bool, str]:
    """按操作符比较实际值与期望值，返回 ``(是否通过, 中文说明)``。

    数值比较在两侧都能转 float 时按数值比较，否则按字符串比较；
    布尔值与 ``"true"`` / ``"false"`` 在 ``eq`` / ``ne`` 下等价（忽略大小写）。
    """
    op = str(operator or "").strip()
    if op not in OPERATORS:
        return False, "未知操作符: %s" % (_fmt(operator),)

    if op == "eq":
        passed = _equal(actual, expected)
        return passed, _pair_message(passed, "等于", "不等于", actual, expected)
    if op == "ne":
        passed = not _equal(actual, expected)
        return passed, _pair_message(passed, "不等于", "等于", actual, expected)
    if op in ("gt", "gte", "lt", "lte"):
        return _compare_order(op, actual, expected)
    if op == "contains":
        return _compare_contains(actual, expected, False)
    if op == "not_contains":
        return _compare_contains(actual, expected, True)
    if op == "starts_with":
        if actual is None:
            return False, "实际值为 null，无法判断前缀"
        passed = str(actual).startswith(str(expected))
        return passed, _pair_message(passed, "以", "不以", actual, expected) + " 开头"
    if op == "ends_with":
        if actual is None:
            return False, "实际值为 null，无法判断后缀"
        passed = str(actual).endswith(str(expected))
        return passed, _pair_message(passed, "以", "不以", actual, expected) + " 结尾"
    if op == "regex":
        pattern = expected if isinstance(expected, str) else str(expected)
        try:
            matched = re.search(pattern, "" if actual is None else str(actual)) is not None
        except re.error as exc:
            return False, '正则表达式无效: "%s"（%s）' % (pattern, exc)
        return matched, "实际值 %s %s正则 %s" % (_fmt(actual), "匹配" if matched else "不匹配", _fmt(pattern))
    if op in ("in", "not_in"):
        if isinstance(expected, (list, tuple)):
            hit = any(_json_equal(item, actual) for item in expected)
            scope = "集合"
        elif isinstance(expected, str):
            hit = str(actual) in expected
            scope = "字符串"
        else:
            return False, "操作符 %s 的期望值必须是数组，实际为 %s" % (op, _fmt(expected))
        passed = (not hit) if op == "not_in" else hit
        positive = "不在" if op == "not_in" else "在"
        negative = "在" if op == "not_in" else "不在"
        message = "实际值 %s %s期望%s %s 中" % (_fmt(actual), positive if passed else negative, scope, _fmt(expected))
        return passed, message
    if op == "exists":
        passed = actual is not None
        if passed:
            return True, "实际值 %s 存在" % _fmt(actual)
        return False, "实际值为 null，期望存在"
    if op == "not_exists":
        if actual is None:
            return True, "实际值为 null，符合不存在期望"
        return False, "实际值 %s 存在，期望不存在" % _fmt(actual)
    if op in ("is_true", "is_false"):
        flag = _truth(actual)
        passed = flag is (op == "is_true")
        word = "真值" if op == "is_true" else "假值"
        if passed:
            return True, "实际值 %s 为%s" % (_fmt(actual), word)
        return False, "实际值 %s 不是%s" % (_fmt(actual), word)
    if op in ("is_empty", "not_empty"):
        empty = _is_empty(actual)
        passed = empty if op == "is_empty" else not empty
        wanted = "为空" if op == "is_empty" else "不为空"
        actual_text = "为空" if empty else "不为空"
        if passed:
            return True, "实际值 %s %s" % (_fmt(actual), actual_text)
        return False, "实际值 %s %s，期望%s" % (_fmt(actual), actual_text, wanted)
    if op == "type_is":
        type_name = str(expected).strip().lower() if expected is not None else ""
        if type_name not in _TYPE_NAMES:
            return False, "type_is 的期望值必须是 %s 之一，实际为 %s" % ("|".join(_TYPE_NAMES), _fmt(expected))
        passed = _matches_type(actual, type_name)
        if passed:
            return True, "实际值 %s 的类型为 %s，符合期望 %s" % (_fmt(actual), _type_name(actual), type_name)
        return False, "实际值 %s 的类型为 %s，期望 %s" % (_fmt(actual), _type_name(actual), type_name)
    if op in ("length_eq", "length_gt", "length_lt"):
        return _compare_length(op, actual, expected)
    if op == "between":
        return _compare_between(actual, expected)
    if op == "approx":
        return _compare_approx(actual, expected, tolerance)
    if op == "json_contains":
        passed = _json_contains(actual, expected)
        if passed:
            return True, "实际值 %s 包含期望子集 %s" % (_fmt(actual), _fmt(expected))
        return False, "实际值 %s 不包含期望子集 %s" % (_fmt(actual), _fmt(expected))
    if op == "json_equals":
        passed = _json_equal(actual, expected)
        if passed:
            return True, "实际值 %s 与期望值 %s 结构相等" % (_fmt(actual), _fmt(expected))
        return False, "实际值 %s 与期望值 %s 结构不相等" % (_fmt(actual), _fmt(expected))
    return False, "未知操作符: %s" % (_fmt(operator),)


# ---------------------------------------------------------------------------
# 求值入口
# ---------------------------------------------------------------------------


def _as_spec(assertion: Any) -> AssertionSpec:
    if isinstance(assertion, AssertionSpec):
        return assertion
    if isinstance(assertion, Mapping):
        return AssertionSpec.from_dict(dict(assertion))
    raise TypeError("断言必须是 AssertionSpec 或 dict，实际为 %s" % type(assertion).__name__)


def _describe(spec: AssertionSpec) -> str:
    try:
        text = spec.describe()
    except Exception:  # pragma: no cover - describe 不应失败
        text = ""
    if text:
        return text
    return "%s %s %s" % (spec.type, spec.operator, spec.expected)


def _append_note(message: str, note: str) -> str:
    if not note:
        return message
    return _join([message, note])


def evaluate(assertion: AssertionSpec, context: AssertionContext, index: int = 0) -> AssertionResult:
    """求值单条断言，永不抛异常。"""
    result = AssertionResult(index=index)
    try:
        spec = _as_spec(assertion)
        result.type = spec.type
        result.operator = spec.operator
        result.expected = spec.expected
        result.describe = _describe(spec)
        result.target = _target_text(spec)
        if not spec.enabled:
            result.passed = True
            result.message = "断言已禁用（enabled=false），跳过校验"
            return result
        target = _extract_target(spec, context)
        result.target = target.target or result.target
        if target.done:
            result.actual = target.value
            result.passed = bool(target.passed)
            result.message = target.message if result.passed else _append_note(target.message, spec.message)
            return result
        if not target.found:
            if spec.operator == "not_exists" and target.no_match:
                result.passed = True
                result.actual = None
                result.message = target.message + "，符合 not_exists 期望"
            else:
                result.passed = False
                result.actual = None
                result.message = _append_note(target.message, spec.message)
            return result
        result.actual = target.value
        passed, message = compare(target.value, spec.operator, spec.expected, spec.tolerance)
        result.passed = passed
        message = _append_note(message, target.message)
        if not passed:
            message = _append_note(message, spec.message)
        result.message = message
    except Exception as exc:  # 兜底：断言求值绝不向上抛异常
        result.passed = False
        result.message = "断言执行异常: %s: %s" % (type(exc).__name__, exc)
    return result


def evaluate_all(assertions: Sequence[AssertionSpec], context: AssertionContext) -> List[AssertionResult]:
    """按顺序求值所有断言，``index`` 为断言在列表中的位置。"""
    if not assertions:
        return []
    return [evaluate(assertion, context, index) for index, assertion in enumerate(assertions)]


# ---------------------------------------------------------------------------
# 默认断言（供导入器生成冒烟用例）
# ---------------------------------------------------------------------------


def _schema_object_info(schema: Any) -> Tuple[Dict[str, Any], List[str]]:
    """从响应 schema 中提取对象属性与必填字段；不像对象时返回空。"""
    if not isinstance(schema, dict):
        return {}, []
    schema_type = schema.get("type")
    if isinstance(schema_type, (list, tuple)):
        if "object" not in [str(item) for item in schema_type]:
            return {}, []
    elif schema_type not in (None, "object"):
        return {}, []
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        properties = {}
    required_raw = schema.get("required")
    required: List[str] = []
    if isinstance(required_raw, (list, tuple)):
        for item in required_raw:
            if isinstance(item, str) and item.strip():
                required.append(item.strip())
    return {str(key): value for key, value in properties.items()}, required


def _path_for(name: str) -> str:
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
        return "$.%s" % name
    return "$['%s']" % name.replace("'", "\\'")


def default_assertions_for(method: str, response_schema: Any = None) -> List[AssertionSpec]:
    """按 HTTP 方法与响应 schema 生成默认冒烟断言（顺序确定）。

    生成规则：

    1. 若响应 schema 是对象且声明了 ``code`` 字段，最前面加 ``$.code eq 0``；
    2. ``status_code in [200, 201, 204]``（``DELETE`` 为 ``[200, 202, 204]``）；
    3. ``response_time lt 3000``；
    4. 若 schema 声明了 ``data`` 字段，加 ``$.data exists``；
    5. 若 schema 声明了 ``required`` 字段，逐个加 ``$.<字段> exists``（最多 5 个，已覆盖的字段跳过）。
    """
    method_name = str(method or "GET").strip().upper()
    statuses = [200, 202, 204] if method_name == "DELETE" else [200, 201, 204]
    properties, required = _schema_object_info(response_schema)

    assertions: List[AssertionSpec] = []
    covered = set()
    if "code" in properties:
        assertions.append(AssertionSpec(type="json_path", operator="eq", expected=0, path="$.code"))
        covered.add("code")
    assertions.append(AssertionSpec(type="status_code", operator="in", expected=statuses))
    assertions.append(AssertionSpec(type="response_time", operator="lt", expected=3000))
    if "data" in properties and "data" not in covered:
        assertions.append(AssertionSpec(type="json_path", operator="exists", expected=None, path="$.data"))
        covered.add("data")
    added = 0
    for name in required:
        if added >= 5:
            break
        if name in covered:
            continue
        assertions.append(AssertionSpec(type="json_path", operator="exists", expected=None, path=_path_for(name)))
        covered.add(name)
        added += 1
    return assertions
