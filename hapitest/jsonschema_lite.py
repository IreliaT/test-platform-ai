"""极简 JSON Schema 校验器（仅标准库，绝不抛异常）。

支持的关键字
------------
``type``（含类型数组）、``required``、``properties``、``items``（对象或数组）、``enum``、
``nullable``、``oneOf``/``anyOf``/``allOf``（尽力而为）、``minimum``/``maximum``、
``minLength``/``maxLength``、``minItems``/``maxItems``、``pattern``。

约定
----
- 返回人类可读的中文错误列表，空列表表示通过；任何情况下都不抛异常。
- ``type`` 支持 ``string`` / ``number`` / ``integer`` / ``boolean`` / ``object`` / ``array`` /
  ``null``；``integer`` 只接受 ``int``（``bool`` 不算 ``integer``，``1.0`` 算 ``number``）；
  布尔值与数字不互相兼容。类型名未知时忽略该类型（避免 OpenAPI 的 ``file`` 等类型误报）。
- ``nullable: true`` 时 ``null`` 直接视为合法，不再做其它校验。
- ``enum`` 使用 JSON 语义的深比较：字典忽略键顺序，``True`` 与 ``1`` 不相等，``1`` 与 ``1.0`` 相等。
- ``oneOf``/``anyOf``：至少一个子模式通过即通过（``oneOf`` 不强制唯一命中，属尽力而为）；
  ``allOf``：所有子模式必须通过，子错误原样合并返回。
- 未知关键字一律静默忽略。
"""

from __future__ import annotations

import json
import re
from typing import Any, List, Sequence

__all__ = ["validate"]

# 错误条数上限：避免超大数组把错误列表撑爆
_MAX_ERRORS = 50

_TYPE_NAMES = ("string", "number", "integer", "boolean", "object", "array", "null")

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _fmt(value: Any) -> str:
    """把取值渲染成简短、确定性的文本，用于错误信息。"""
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
    if len(text) > 120:
        text = text[:120] + "…"
    return text


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


def _child_path(path: str, name: Any) -> str:
    text = str(name)
    if _IDENTIFIER_RE.match(text):
        return "%s.%s" % (path, text)
    return "%s['%s']" % (path, text.replace("'", "\\'"))


def _index_path(path: str, index: int) -> str:
    return "%s[%d]" % (path, index)


def _matches_type(instance: Any, type_name: str) -> bool:
    if type_name == "string":
        return isinstance(instance, str)
    if type_name == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    if type_name == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if type_name == "boolean":
        return isinstance(instance, bool)
    if type_name == "object":
        return isinstance(instance, dict)
    if type_name == "array":
        return isinstance(instance, (list, tuple))
    if type_name == "null":
        return instance is None
    return True  # 未知类型名：不参与校验


def _json_equal(left: Any, right: Any) -> bool:
    """JSON 语义深比较：字典忽略键顺序，布尔与数字严格区分。"""
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


def _type_error(path: str, schema_type: Any, instance: Any) -> str:
    if isinstance(schema_type, (list, tuple)):
        expected = " 或 ".join(str(item) for item in schema_type)
    else:
        expected = str(schema_type)
    return "%s: 类型应为 %s，实际为 %s（取值 %s）" % (path, expected, _type_name(instance), _fmt(instance))


def _validate(instance: Any, schema: Any, path: str, errors: List[str]) -> None:
    if len(errors) >= _MAX_ERRORS:
        return
    if schema is True or schema is None:
        return
    if schema is False:
        errors.append("%s: JSON Schema 为 false，任何取值都不合法" % path)
        return
    if not isinstance(schema, dict):
        errors.append("%s: JSON Schema 必须是对象或布尔值，实际为 %s" % (path, type(schema).__name__))
        return

    # nullable：null 直接合法
    if instance is None and schema.get("nullable") is True:
        return

    # type
    schema_type = schema.get("type")
    if schema_type is not None:
        candidates: Sequence[Any]
        if isinstance(schema_type, (list, tuple)):
            candidates = list(schema_type)
        else:
            candidates = [schema_type]
        if candidates and not any(_matches_type(instance, str(item)) for item in candidates):
            errors.append(_type_error(path, schema_type, instance))
            return

    # enum
    allowed = schema.get("enum")
    if isinstance(allowed, (list, tuple)) and not any(_json_equal(instance, item) for item in allowed):
        errors.append(
            "%s: 取值必须是 %s 之一，实际为 %s"
            % (path, "、".join(_fmt(item) for item in allowed) or "（空枚举）", _fmt(instance))
        )

    # 数值范围
    minimum = schema.get("minimum")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool) and isinstance(minimum, (int, float)):
        if instance < minimum:
            errors.append("%s: 数值应大于等于 %s，实际为 %s" % (path, _fmt(minimum), _fmt(instance)))
    maximum = schema.get("maximum")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool) and isinstance(maximum, (int, float)):
        if instance > maximum:
            errors.append("%s: 数值应小于等于 %s，实际为 %s" % (path, _fmt(maximum), _fmt(instance)))

    # 字符串约束
    if isinstance(instance, str):
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and not isinstance(min_length, bool) and len(instance) < min_length:
            errors.append("%s: 字符串长度应大于等于 %d，实际为 %d" % (path, min_length, len(instance)))
        max_length = schema.get("maxLength")
        if isinstance(max_length, int) and not isinstance(max_length, bool) and len(instance) > max_length:
            errors.append("%s: 字符串长度应小于等于 %d，实际为 %d" % (path, max_length, len(instance)))
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and pattern:
            try:
                matched = re.search(pattern, instance) is not None
            except re.error as exc:
                errors.append('%s: pattern 不是合法正则 "%s"（%s）' % (path, pattern, exc))
            else:
                if not matched:
                    errors.append('%s: 字符串不匹配正则 "%s"' % (path, pattern))

    # 数组约束
    if isinstance(instance, (list, tuple)):
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and not isinstance(min_items, bool) and len(instance) < min_items:
            errors.append("%s: 数组长度应大于等于 %d，实际为 %d" % (path, min_items, len(instance)))
        max_items = schema.get("maxItems")
        if isinstance(max_items, int) and not isinstance(max_items, bool) and len(instance) > max_items:
            errors.append("%s: 数组长度应小于等于 %d，实际为 %d" % (path, max_items, len(instance)))
        items = schema.get("items")
        if isinstance(items, dict) or isinstance(items, bool):
            for index, item in enumerate(instance):
                _validate(item, items, _index_path(path, index), errors)
                if len(errors) >= _MAX_ERRORS:
                    break
        elif isinstance(items, (list, tuple)):
            for index, item_schema in enumerate(items):
                if index >= len(instance):
                    break
                _validate(instance[index], item_schema, _index_path(path, index), errors)
                if len(errors) >= _MAX_ERRORS:
                    break

    # 对象约束
    if isinstance(instance, dict):
        required = schema.get("required")
        if isinstance(required, (list, tuple)):
            for name in required:
                if not isinstance(name, (str, int, float)) or isinstance(name, bool):
                    continue
                if str(name) not in instance:
                    errors.append('%s: 缺少必填字段 "%s"' % (path, name))
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for name, sub_schema in properties.items():
                key = str(name)
                if key in instance:
                    _validate(instance[key], sub_schema, _child_path(path, key), errors)

    # 组合关键字（尽力而为）
    all_of = schema.get("allOf")
    if isinstance(all_of, (list, tuple)):
        for sub_schema in all_of:
            _validate(instance, sub_schema, path, errors)

    any_of = schema.get("anyOf")
    if isinstance(any_of, (list, tuple)) and any_of:
        if not any(not validate(instance, sub_schema, path) for sub_schema in any_of):
            errors.append("%s: 不满足 anyOf（%d 个候选模式均未通过）" % (path, len(any_of)))

    one_of = schema.get("oneOf")
    if isinstance(one_of, (list, tuple)) and one_of:
        if not any(not validate(instance, sub_schema, path) for sub_schema in one_of):
            errors.append("%s: 不满足 oneOf（%d 个候选模式均未通过）" % (path, len(one_of)))


def validate(instance: Any, schema: Any, path: str = "$") -> List[str]:
    """校验 ``instance`` 是否满足 ``schema``，返回中文错误列表（空表示通过）。

    :param path: 当前取值在文档中的位置，用于拼装错误信息，默认 ``$``。
    """
    errors: List[str] = []
    try:
        _validate(instance, schema, path if isinstance(path, str) and path else "$", errors)
    except Exception as exc:  # 兜底：任何意外都转成错误信息，绝不抛异常
        return ['%s: Schema 校验内部错误: %s: %s' % (path, type(exc).__name__, exc)]
    if len(errors) >= _MAX_ERRORS:
        errors.append("（错误过多，仅显示前 %d 条）" % _MAX_ERRORS)
    return errors
