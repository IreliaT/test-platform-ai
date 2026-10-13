"""``hapitest.jsonpath`` 的单元测试：覆盖文档化的全部语法子集与错误分支。"""

from __future__ import annotations

import unittest

from hapitest.jsonpath import JsonPathError, exists, first, query

DOC = {
    "code": 0,
    "msg": "ok",
    "data": {
        "id": 42,
        "name": "hapitest",
        "items": [1, 2, 3],
        "nested": {"id": 7, "token": "abc"},
        "list": [
            {"id": 1, "status": 1, "tag": "a b"},
            {"id": 2, "status": 2},
            {"id": 3, "status": 1, "active": True},
        ],
        "flags": {"on": True, "off": False, "none": None},
    },
    "arr": [10, 20, 30, 40, 50],
    "a b": {"c": 1},
}

RECURSIVE = {
    "id": 1,
    "child": {"id": 2, "child": {"id": 3}},
    "items": [{"id": 4}, {"nested": {"id": 5}}],
}


class RootTest(unittest.TestCase):
    def test_root_returns_document(self) -> None:
        self.assertEqual(query(DOC, "$"), [DOC])
        self.assertEqual(query([1, 2], "$"), [[1, 2]])
        self.assertEqual(query(None, "$"), [None])

    def test_empty_expression_returns_document(self) -> None:
        self.assertEqual(query(DOC, ""), [DOC])
        self.assertEqual(query(DOC, "   "), [DOC])

    def test_non_string_expression_raises(self) -> None:
        with self.assertRaises(JsonPathError) as ctx:
            query(DOC, 123)  # type: ignore[arg-type]
        self.assertEqual(str(ctx.exception), "JSONPath 表达式必须是字符串，实际为 int")


class ChildTest(unittest.TestCase):
    def test_dot_child(self) -> None:
        self.assertEqual(query(DOC, "$.code"), [0])
        self.assertEqual(query(DOC, "$.data.name"), ["hapitest"])

    def test_implicit_root(self) -> None:
        self.assertEqual(query(DOC, "code"), [0])
        self.assertEqual(query(DOC, "data.nested.id"), [7])

    def test_bracket_single_quote(self) -> None:
        self.assertEqual(query(DOC, "$['data']['name']"), ["hapitest"])
        self.assertEqual(query(DOC, "$['a b'].c"), [1])

    def test_bracket_double_quote(self) -> None:
        self.assertEqual(query(DOC, '$["data"]["name"]'), ["hapitest"])
        self.assertEqual(query(DOC, 'data["nested"]["token"]'), ["abc"])

    def test_implicit_root_with_bracket(self) -> None:
        self.assertEqual(query(DOC, "['a b'].c"), [1])

    def test_missing_field_yields_no_match(self) -> None:
        self.assertEqual(query(DOC, "$.missing"), [])
        self.assertEqual(query(DOC, "$.missing.deeper"), [])
        self.assertEqual(query(DOC, "$.data.items.nope"), [])

    def test_child_on_scalar_yields_no_match(self) -> None:
        self.assertEqual(query(DOC, "$.code.child"), [])
        self.assertEqual(query(DOC, "$.data.items.child"), [])

    def test_child_on_list_yields_no_match(self) -> None:
        self.assertEqual(query(DOC, "$.data.items[0].x"), [])


class IndexTest(unittest.TestCase):
    def test_positive_index(self) -> None:
        self.assertEqual(query(DOC, "$.arr[0]"), [10])
        self.assertEqual(query(DOC, "$.arr[4]"), [50])
        self.assertEqual(query(DOC, "$.data.items[1]"), [2])

    def test_negative_index(self) -> None:
        self.assertEqual(query(DOC, "$.arr[-1]"), [50])
        self.assertEqual(query(DOC, "$.arr[-5]"), [10])

    def test_out_of_range(self) -> None:
        self.assertEqual(query(DOC, "$.arr[5]"), [])
        self.assertEqual(query(DOC, "$.arr[-6]"), [])
        self.assertEqual(query(DOC, "$.code[0]"), [])

    def test_index_on_object_yields_no_match(self) -> None:
        self.assertEqual(query(DOC, "$.data[0]"), [])

    def test_index_then_child(self) -> None:
        self.assertEqual(query(DOC, "$.data.list[0].id"), [1])


class WildcardTest(unittest.TestCase):
    def test_wildcard_on_list(self) -> None:
        self.assertEqual(query(DOC, "$.data.items[*]"), [1, 2, 3])
        self.assertEqual(query(DOC, "$.data.items.*"), [1, 2, 3])

    def test_wildcard_on_object(self) -> None:
        self.assertEqual(query(DOC, "$.data.flags.*"), [True, False, None])
        self.assertEqual(query(DOC, "$.data.nested[*]"), [7, "abc"])

    def test_wildcard_then_child(self) -> None:
        self.assertEqual(query(DOC, "$.data.list[*].id"), [1, 2, 3])

    def test_wildcard_on_scalar(self) -> None:
        self.assertEqual(query(DOC, "$.code[*]"), [])


class RecursiveTest(unittest.TestCase):
    def test_recursive_depth_first_including_lists(self) -> None:
        self.assertEqual(query(RECURSIVE, "$..id"), [1, 2, 3, 4, 5])

    def test_recursive_includes_root_level_field(self) -> None:
        self.assertEqual(query({"id": 9, "child": {"id": 8}}, "$..id"), [9, 8])

    def test_recursive_no_match(self) -> None:
        self.assertEqual(query(RECURSIVE, "$..nope"), [])

    def test_recursive_wildcard(self) -> None:
        data = {"a": {"b": 1}, "c": [2, {"d": 3}]}
        self.assertEqual(query(data, "$..*"), [{"b": 1}, 1, [2, {"d": 3}], 2, {"d": 3}, 3])

    def test_recursive_with_child(self) -> None:
        data = {"items": [{"id": 1}, {"id": 2}]}
        self.assertEqual(query(data, "$..id"), [1, 2])


class SliceTest(unittest.TestCase):
    def test_start_stop(self) -> None:
        self.assertEqual(query(DOC, "$.arr[1:3]"), [20, 30])

    def test_open_stop(self) -> None:
        self.assertEqual(query(DOC, "$.arr[1:]"), [20, 30, 40, 50])

    def test_open_start(self) -> None:
        self.assertEqual(query(DOC, "$.arr[:2]"), [10, 20])

    def test_step(self) -> None:
        self.assertEqual(query(DOC, "$.arr[::2]"), [10, 30, 50])
        self.assertEqual(query(DOC, "$.arr[1::2]"), [20, 40])

    def test_negative_step(self) -> None:
        self.assertEqual(query(DOC, "$.arr[::-1]"), [50, 40, 30, 20, 10])

    def test_negative_indices(self) -> None:
        self.assertEqual(query(DOC, "$.arr[-2:]"), [40, 50])
        self.assertEqual(query(DOC, "$.arr[:-3]"), [10, 20])

    def test_slice_out_of_range_is_empty(self) -> None:
        self.assertEqual(query(DOC, "$.arr[9:12]"), [])

    def test_slice_on_non_list(self) -> None:
        self.assertEqual(query(DOC, "$.code[0:2]"), [])


class FilterTest(unittest.TestCase):
    def test_equal_number(self) -> None:
        self.assertEqual(query(DOC, "$.data.list[?(@.status == 1)].id"), [1, 3])

    def test_not_equal(self) -> None:
        self.assertEqual(query(DOC, "$.data.list[?(@.status != 1)].id"), [2])

    def test_greater_and_greater_equal(self) -> None:
        self.assertEqual(query(DOC, "$.data.list[?(@.id > 2)].id"), [3])
        self.assertEqual(query(DOC, "$.data.list[?(@.id >= 2)].id"), [2, 3])

    def test_less_and_less_equal(self) -> None:
        self.assertEqual(query(DOC, "$.data.list[?(@.id < 2)].id"), [1])
        self.assertEqual(query(DOC, "$.data.list[?(@.id <= 2)].id"), [1, 2])

    def test_regex_operator(self) -> None:
        self.assertEqual(query(DOC, "$.data.list[?(@.tag =~ '^a')].id"), [1])
        self.assertEqual(query(DOC, "$.data.list[?(@.tag =~ '^b')].id"), [])
        self.assertEqual(query(DOC, "$.data.list[?(@.tag =~ 'b$')].id"), [1])

    def test_and_operator(self) -> None:
        self.assertEqual(query(DOC, "$.data.list[?(@.status == 1 && @.id > 1)].id"), [3])

    def test_or_operator(self) -> None:
        self.assertEqual(query(DOC, "$.data.list[?(@.status == 2 || @.id == 1)].id"), [1, 2])

    def test_parentheses(self) -> None:
        self.assertEqual(query(DOC, "$.data.list[?((@.status == 1) && (@.id <= 1))].id"), [1])

    def test_quoted_left_hand_path(self) -> None:
        self.assertEqual(query(DOC, '$.data.list[?(@["tag"] == "a b")].id'), [1])

    def test_nested_left_hand_path(self) -> None:
        data = {"rows": [{"user": {"age": 20}}, {"user": {"age": 30}}]}
        self.assertEqual(query(data, "$.rows[?(@.user.age >= 30)]"), [{"user": {"age": 30}}])

    def test_existence_filter(self) -> None:
        self.assertEqual(query(DOC, "$.data.list[?(@.tag)].id"), [1])
        self.assertEqual(query(DOC, "$.data.list[?(@.active)]").__len__(), 1)

    def test_existence_filter_with_child(self) -> None:
        data = {"a": [{"b": 1, "c": 2}, {"c": 3}, {"b": 0, "c": 4}]}
        self.assertEqual(query(data, "$.a[?(@.b)].c"), [2, 4])

    def test_boolean_and_null_literals(self) -> None:
        self.assertEqual(query(DOC, "$.data.list[?(@.active == true)].id"), [3])
        self.assertEqual(query(DOC, "$.data.list[?(@.missing == null)]"), [])
        self.assertEqual(query(DOC, "$.data.flags[?(@ == null)]"), [None])
        self.assertEqual(query(DOC, "$.data.flags[?(@ == false)]"), [False])

    def test_string_literal_double_quote(self) -> None:
        self.assertEqual(query(DOC, '$.data.list[?(@.tag == "a b")].id'), [1])

    def test_negative_number_literal(self) -> None:
        data = {"rows": [{"v": -5}, {"v": 5}]}
        self.assertEqual(query(data, "$.rows[?(@.v < -1)]"), [{"v": -5}])

    def test_no_match_is_empty(self) -> None:
        self.assertEqual(query(DOC, "$.data.list[?(@.status == 99)]"), [])

    def test_missing_field_never_matches(self) -> None:
        self.assertEqual(query(DOC, "$.data.list[?(@.nope == 1)]"), [])

    def test_filter_on_object_values(self) -> None:
        data = {"m": {"a": {"x": 1}, "b": {"x": 2}}}
        self.assertEqual(query(data, "$.m[?(@.x == 2)]"), [{"x": 2}])

    def test_filter_on_scalar_is_empty(self) -> None:
        self.assertEqual(query(DOC, "$.code[?(@ == 1)]"), [])

    def test_not_operator(self) -> None:
        self.assertEqual(query(DOC, "$.data.list[?(!@.tag)].id"), [2, 3])


class ErrorTest(unittest.TestCase):
    def test_unclosed_bracket(self) -> None:
        with self.assertRaises(JsonPathError) as ctx:
            query(DOC, "$.a[")
        self.assertIn('"[" 未闭合', str(ctx.exception))
        self.assertIn('表达式 "$.a["', str(ctx.exception))

    def test_unclosed_quote(self) -> None:
        with self.assertRaises(JsonPathError) as ctx:
            query(DOC, "$['a]")
        self.assertIn("字符串未闭合", str(ctx.exception))

    def test_unknown_fragment_message_is_exact(self) -> None:
        with self.assertRaises(JsonPathError) as ctx:
            query(DOC, "$foo")
        self.assertEqual(
            str(ctx.exception),
            'JSONPath 语法错误：无法识别的片段（应以 "." 或 "[" 继续）（位置 1，片段 "foo"，表达式 "$foo"）',
        )

    def test_extra_fragment_after_path(self) -> None:
        with self.assertRaises(JsonPathError) as ctx:
            query(DOC, "$.a[0] x")
        self.assertIn("无法识别的片段", str(ctx.exception))

    def test_missing_name_after_dot(self) -> None:
        with self.assertRaises(JsonPathError) as ctx:
            query(DOC, "$.")
        self.assertIn("缺少字段名", str(ctx.exception))

    def test_dot_before_bracket(self) -> None:
        with self.assertRaises(JsonPathError) as ctx:
            query(DOC, "$.[0]")
        self.assertIn('"." 后缺少字段名', str(ctx.exception))

    def test_bad_index(self) -> None:
        with self.assertRaises(JsonPathError) as ctx:
            query(DOC, "$.a[abc]")
        self.assertIn("无法解析的下标", str(ctx.exception))

    def test_bad_slice_argument(self) -> None:
        with self.assertRaises(JsonPathError) as ctx:
            query(DOC, "$.a[1:x]")
        self.assertIn("切片参数必须是整数", str(ctx.exception))

    def test_zero_step(self) -> None:
        with self.assertRaises(JsonPathError) as ctx:
            query(DOC, "$.a[1:5:0]")
        self.assertIn("切片步长不能为 0", str(ctx.exception))

    def test_filter_without_parenthesis(self) -> None:
        with self.assertRaises(JsonPathError) as ctx:
            query(DOC, "$.a[?@.b == 1]")
        self.assertIn('过滤器必须以 "[?(" 开头', str(ctx.exception))

    def test_filter_incomplete_expression(self) -> None:
        with self.assertRaises(JsonPathError) as ctx:
            query(DOC, "$.a[?(@.b ==)]")
        self.assertIn("过滤器只支持", str(ctx.exception))

    def test_filter_bad_operator(self) -> None:
        with self.assertRaises(JsonPathError) as ctx:
            query(DOC, "$.a[?(@.b ~~ 1)]")
        self.assertIn('期望 ")"', str(ctx.exception))

    def test_filter_unclosed(self) -> None:
        with self.assertRaises(JsonPathError) as ctx:
            query(DOC, "$.a[?(@.b == 1]")
        self.assertIn('期望 ")"', str(ctx.exception))

    def test_filter_bad_literal(self) -> None:
        with self.assertRaises(JsonPathError) as ctx:
            query(DOC, "$.a[?(@.b == abc)]")
        self.assertIn("过滤器只支持", str(ctx.exception))

    def test_errors_are_value_error_subclass(self) -> None:
        self.assertTrue(issubclass(JsonPathError, ValueError))


class FirstAndExistsTest(unittest.TestCase):
    def test_first_returns_first_match(self) -> None:
        self.assertEqual(first(DOC, "$.data.list[*].id"), 1)

    def test_first_returns_default_when_missing(self) -> None:
        self.assertIsNone(first(DOC, "$.nope"))
        self.assertEqual(first(DOC, "$.nope", "fallback"), "fallback")

    def test_first_keeps_null_value(self) -> None:
        self.assertIsNone(first(DOC, "$.data.flags.none", "fallback"))

    def test_exists(self) -> None:
        self.assertTrue(exists(DOC, "$.data.id"))
        self.assertFalse(exists(DOC, "$.data.nope"))
        self.assertTrue(exists(DOC, "$.data.flags.none"))

    def test_exists_propagates_syntax_error(self) -> None:
        with self.assertRaises(JsonPathError):
            exists(DOC, "$.a[")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
