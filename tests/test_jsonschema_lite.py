"""``hapitest.jsonschema_lite`` 的单元测试：覆盖全部支持的关键字与「绝不抛异常」约定。"""

from __future__ import annotations

import unittest

from hapitest.jsonschema_lite import validate


class _Boom:
    """比较时抛异常，用于验证校验器兜底。"""

    def __eq__(self, other: object) -> bool:
        raise RuntimeError("boom")

    __hash__ = None  # type: ignore[assignment]


class TypeTest(unittest.TestCase):
    def test_supported_types_pass(self) -> None:
        self.assertEqual(validate("a", {"type": "string"}), [])
        self.assertEqual(validate(1, {"type": "number"}), [])
        self.assertEqual(validate(1.5, {"type": "number"}), [])
        self.assertEqual(validate(1, {"type": "integer"}), [])
        self.assertEqual(validate(True, {"type": "boolean"}), [])
        self.assertEqual(validate({}, {"type": "object"}), [])
        self.assertEqual(validate([], {"type": "array"}), [])
        self.assertEqual(validate(None, {"type": "null"}), [])

    def test_type_mismatch_messages(self) -> None:
        self.assertEqual(
            validate(1, {"type": "string"}),
            ["$: 类型应为 string，实际为 integer（取值 1）"],
        )
        self.assertEqual(
            validate("1", {"type": "number"}),
            ['$: 类型应为 number，实际为 string（取值 "1"）'],
        )
        self.assertEqual(
            validate(True, {"type": "integer"}),
            ["$: 类型应为 integer，实际为 boolean（取值 true）"],
        )
        self.assertEqual(
            validate(1.0, {"type": "integer"}),
            ["$: 类型应为 integer，实际为 number（取值 1.0）"],
        )
        self.assertEqual(
            validate(True, {"type": "number"}),
            ["$: 类型应为 number，实际为 boolean（取值 true）"],
        )
        self.assertEqual(
            validate(None, {"type": "object"}),
            ["$: 类型应为 object，实际为 null（取值 null）"],
        )

    def test_number_and_integer_are_distinct(self) -> None:
        self.assertEqual(validate(1, {"type": "integer"}), [])
        self.assertEqual(len(validate(1.0, {"type": "integer"})), 1)
        self.assertEqual(validate(1.0, {"type": "number"}), [])
        self.assertEqual(validate(True, {"type": "boolean"}), [])
        self.assertEqual(len(validate(True, {"type": "string"})), 1)

    def test_type_list(self) -> None:
        schema = {"type": ["string", "null"]}
        self.assertEqual(validate("a", schema), [])
        self.assertEqual(validate(None, schema), [])
        self.assertEqual(validate(1, schema), ["$: 类型应为 string 或 null，实际为 integer（取值 1）"])

    def test_unknown_type_name_is_ignored(self) -> None:
        self.assertEqual(validate("x", {"type": "file"}), [])
        self.assertEqual(validate(1, {"type": ["string", "file"]}), [])

    def test_type_mismatch_stops_further_checks(self) -> None:
        schema = {"type": "string", "minLength": 10}
        self.assertEqual(validate(1, schema), ["$: 类型应为 string，实际为 integer（取值 1）"])

    def test_missing_type_skips_type_check(self) -> None:
        self.assertEqual(validate("x", {"minLength": 1}), [])


class RequiredTest(unittest.TestCase):
    def test_required_present(self) -> None:
        schema = {"type": "object", "required": ["a", "b"]}
        self.assertEqual(validate({"a": 1, "b": 2}, schema), [])
        self.assertEqual(validate({"a": 1, "b": None}, schema), [])

    def test_required_missing(self) -> None:
        schema = {"type": "object", "required": ["a", "b"]}
        self.assertEqual(validate({"a": 1}, schema), ['$: 缺少必填字段 "b"'])

    def test_required_ignored_for_non_object(self) -> None:
        self.assertEqual(validate("x", {"required": ["a"]}), [])

    def test_required_ignores_invalid_entries(self) -> None:
        # 数字会被当成字段名的字符串形式；None / 布尔不是合法字段名，直接跳过
        self.assertEqual(
            validate({}, {"required": [1, None, True, "a"]}),
            ['$: 缺少必填字段 "1"', '$: 缺少必填字段 "a"'],
        )


class PropertiesTest(unittest.TestCase):
    def test_nested_properties_path(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "data": {"type": "object", "properties": {"id": {"type": "integer"}}},
            },
        }
        self.assertEqual(
            validate({"data": {"id": "x"}}, schema),
            ['$.data.id: 类型应为 integer，实际为 string（取值 "x"）'],
        )

    def test_unknown_properties_are_ignored(self) -> None:
        schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
        self.assertEqual(validate({"a": 1, "extra": "whatever"}, schema), [])

    def test_missing_property_is_not_required_by_properties(self) -> None:
        schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
        self.assertEqual(validate({}, schema), [])

    def test_special_property_name_uses_bracket_path(self) -> None:
        schema = {"type": "object", "properties": {"a-b": {"type": "string"}}}
        self.assertEqual(
            validate({"a-b": 1}, schema),
            ["$['a-b']: 类型应为 string，实际为 integer（取值 1）"],
        )

    def test_properties_skipped_when_instance_is_not_object(self) -> None:
        schema = {"properties": {"a": {"type": "integer"}}}
        self.assertEqual(validate([1], schema), [])


class ItemsTest(unittest.TestCase):
    def test_items_schema(self) -> None:
        schema = {"type": "array", "items": {"type": "integer"}}
        self.assertEqual(validate([1, 2], schema), [])
        self.assertEqual(
            validate([1, "a"], schema),
            ['$[1]: 类型应为 integer，实际为 string（取值 "a"）'],
        )

    def test_items_list(self) -> None:
        schema = {"type": "array", "items": [{"type": "integer"}, {"type": "string"}]}
        self.assertEqual(validate([1, "a"], schema), [])
        self.assertEqual(
            validate([1, 2], schema),
            ["$[1]: 类型应为 string，实际为 integer（取值 2）"],
        )

    def test_items_list_ignores_extra_elements(self) -> None:
        schema = {"items": [{"type": "integer"}]}
        self.assertEqual(validate([1, "x", {}], schema), [])

    def test_items_list_ignores_missing_elements(self) -> None:
        schema = {"items": [{"type": "integer"}, {"type": "string"}]}
        self.assertEqual(validate([1], schema), [])

    def test_nested_items_path(self) -> None:
        schema = {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "integer"}}}}
        self.assertEqual(
            validate([{"id": 1}, {"id": "x"}], schema),
            ['$[1].id: 类型应为 integer，实际为 string（取值 "x"）'],
        )

    def test_items_skipped_when_instance_is_not_array(self) -> None:
        schema = {"items": {"type": "integer"}}
        self.assertEqual(validate("x", schema), [])


class EnumTest(unittest.TestCase):
    def test_enum_pass_and_fail(self) -> None:
        schema = {"enum": ["a", "b"]}
        self.assertEqual(validate("a", schema), [])
        self.assertEqual(validate("x", schema), ['$: 取值必须是 "a"、"b" 之一，实际为 "x"'])

    def test_enum_distinguishes_boolean_and_number(self) -> None:
        self.assertEqual(len(validate(True, {"enum": [1, 2]})), 1)
        self.assertEqual(validate(1, {"enum": [1, 2]}), [])
        self.assertEqual(validate(1.0, {"enum": [1, 2]}), [])

    def test_enum_deep_equality_ignores_key_order(self) -> None:
        schema = {"enum": [{"a": 1, "b": 2}]}
        self.assertEqual(validate({"b": 2, "a": 1}, schema), [])
        self.assertEqual(len(validate({"a": 1}, schema)), 1)

    def test_enum_list_order_matters(self) -> None:
        schema = {"enum": [[1, 2]]}
        self.assertEqual(validate([1, 2], schema), [])
        self.assertEqual(len(validate([2, 1], schema)), 1)

    def test_empty_enum_message(self) -> None:
        self.assertEqual(validate(1, {"enum": []}), ["$: 取值必须是 （空枚举） 之一，实际为 1"])


class NullableTest(unittest.TestCase):
    def test_nullable_allows_null(self) -> None:
        self.assertEqual(validate(None, {"type": "string", "nullable": True}), [])
        self.assertEqual(validate(None, {"type": "string", "nullable": True, "minLength": 3}), [])

    def test_nullable_false_rejects_null(self) -> None:
        self.assertEqual(
            validate(None, {"type": "string", "nullable": False}),
            ["$: 类型应为 string，实际为 null（取值 null）"],
        )

    def test_type_list_with_null(self) -> None:
        self.assertEqual(validate(None, {"type": ["string", "null"]}), [])


class CompositionTest(unittest.TestCase):
    def test_all_of_must_all_pass(self) -> None:
        schema = {"allOf": [{"type": "string"}, {"minLength": 5}]}
        self.assertEqual(validate("abcdef", schema), [])
        self.assertEqual(validate("abc", schema), ["$: 字符串长度应大于等于 5，实际为 3"])

    def test_any_of_at_least_one_passes(self) -> None:
        schema = {"anyOf": [{"type": "string"}, {"type": "integer"}]}
        self.assertEqual(validate("a", schema), [])
        self.assertEqual(validate(1, schema), [])
        self.assertEqual(validate(True, schema), ["$: 不满足 anyOf（2 个候选模式均未通过）"])

    def test_one_of_at_least_one_passes(self) -> None:
        schema = {"oneOf": [{"type": "string"}, {"type": "integer"}]}
        self.assertEqual(validate(1, schema), [])
        self.assertEqual(validate(True, schema), ["$: 不满足 oneOf（2 个候选模式均未通过）"])

    def test_composition_uses_same_path(self) -> None:
        schema = {"properties": {"data": {"anyOf": [{"type": "string"}]}}}
        self.assertEqual(validate({"data": 1}, schema), ["$.data: 不满足 anyOf（1 个候选模式均未通过）"])

    def test_empty_composition_is_ignored(self) -> None:
        self.assertEqual(validate(1, {"anyOf": [], "oneOf": []}), [])


class ConstraintsTest(unittest.TestCase):
    def test_minimum_and_maximum(self) -> None:
        schema = {"minimum": 1, "maximum": 3}
        self.assertEqual(validate(2, schema), [])
        self.assertEqual(validate(0, schema), ["$: 数值应大于等于 1，实际为 0"])
        self.assertEqual(validate(5, schema), ["$: 数值应小于等于 3，实际为 5"])

    def test_min_max_ignored_for_non_number(self) -> None:
        self.assertEqual(validate("x", {"minimum": 1, "maximum": 0}), [])

    def test_string_length(self) -> None:
        self.assertEqual(validate("abc", {"minLength": 3, "maxLength": 3}), [])
        self.assertEqual(
            validate("ab", {"minLength": 3}),
            ["$: 字符串长度应大于等于 3，实际为 2"],
        )
        self.assertEqual(
            validate("abcd", {"maxLength": 3}),
            ["$: 字符串长度应小于等于 3，实际为 4"],
        )

    def test_array_length(self) -> None:
        self.assertEqual(validate([1, 2], {"minItems": 2, "maxItems": 2}), [])
        self.assertEqual(validate([1], {"minItems": 2}), ["$: 数组长度应大于等于 2，实际为 1"])
        self.assertEqual(validate([1, 2, 3], {"maxItems": 2}), ["$: 数组长度应小于等于 2，实际为 3"])

    def test_length_ignored_for_wrong_type(self) -> None:
        self.assertEqual(validate(1, {"minLength": 3}), [])
        self.assertEqual(validate("x", {"minItems": 3}), [])

    def test_pattern(self) -> None:
        self.assertEqual(validate("abc123", {"pattern": "^[a-z]+[0-9]+$"}), [])
        self.assertEqual(
            validate("abc", {"pattern": "^[0-9]+$"}),
            ['$: 字符串不匹配正则 "^[0-9]+$"'],
        )

    def test_invalid_pattern_reports_error(self) -> None:
        errors = validate("abc", {"pattern": "("})
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith('$: pattern 不是合法正则 "("'), errors[0])

    def test_empty_pattern_is_ignored(self) -> None:
        self.assertEqual(validate("abc", {"pattern": ""}), [])


class RobustnessTest(unittest.TestCase):
    def test_unknown_keywords_are_ignored(self) -> None:
        schema = {"type": "object", "x-custom": 1, "additionalProperties": False, "format": "date"}
        self.assertEqual(validate({"a": 1}, schema), [])

    def test_boolean_schemas(self) -> None:
        self.assertEqual(validate(1, True), [])
        self.assertEqual(validate(1, False), ["$: JSON Schema 为 false，任何取值都不合法"])

    def test_none_schema_is_ignored(self) -> None:
        self.assertEqual(validate(1, None), [])

    def test_non_object_schema_reports_error(self) -> None:
        self.assertEqual(validate(1, "nope"), ["$: JSON Schema 必须是对象或布尔值，实际为 str"])

    def test_custom_root_path(self) -> None:
        self.assertEqual(
            validate("x", {"type": "integer"}, path="$.data"),
            ['$.data: 类型应为 integer，实际为 string（取值 "x"）'],
        )

    def test_never_raises(self) -> None:
        errors = validate(_Boom(), {"enum": [_Boom()]})
        self.assertEqual(len(errors), 1)
        self.assertIn("Schema 校验内部错误", errors[0])
        self.assertIn("boom", errors[0])

    def test_error_list_is_capped(self) -> None:
        schema = {"type": "array", "items": {"type": "integer"}}
        errors = validate(["x"] * 80, schema)
        self.assertEqual(len(errors), 51)
        self.assertEqual(errors[-1], "（错误过多，仅显示前 50 条）")

    def test_empty_schema_passes(self) -> None:
        self.assertEqual(validate({"anything": [1, 2]}, {}), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
