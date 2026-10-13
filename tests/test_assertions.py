"""``hapitest.assertions`` 的单元测试。

覆盖：上下文构造、全部操作符、全部断言类型、取值失败分支、默认断言生成。
"""

from __future__ import annotations

import json
import unittest
from typing import Any, Dict, List

from hapitest.assertions import (
    AssertionContext,
    build_context,
    compare,
    default_assertions_for,
    evaluate,
    evaluate_all,
)
from hapitest.models import AssertionSpec, HttpResponse

BODY = {
    "code": 0,
    "msg": "ok",
    "data": {
        "id": 42,
        "name": "hapitest",
        "items": [1, 2, 3],
        "nested": {"token": "abc"},
        "list": [{"id": 1, "status": 1}, {"id": 2, "status": 2}],
    },
}


def make_response(**overrides: Any) -> HttpResponse:
    values: Dict[str, Any] = {
        "ok": True,
        "status": 200,
        "headers": {
            "Content-Type": "application/json; charset=utf-8",
            "X-Trace-Id": "t-1",
            "Set-Cookie": "sid=abc123; Path=/",
        },
        "body_text": json.dumps(BODY, ensure_ascii=False),
        "duration_ms": 123,
        "url": "http://127.0.0.1/api/x",
        "method": "GET",
        "request_preview": {"headers": {"Cookie": "pre=1"}},
    }
    values.update(overrides)
    return HttpResponse(**values)


def spec(**kwargs: Any) -> AssertionSpec:
    return AssertionSpec(**kwargs)


def summaries(specs: List[AssertionSpec]) -> List[tuple]:
    return [
        (item.type, item.operator, item.expected, item.path, item.name, item.tolerance, item.message, item.enabled)
        for item in specs
    ]


class BuildContextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.context = build_context(make_response(), {"token": "T1"})

    def test_basic_fields(self) -> None:
        self.assertEqual(self.context.status, 200)
        self.assertEqual(self.context.duration_ms, 123)
        self.assertEqual(self.context.url, "http://127.0.0.1/api/x")
        self.assertEqual(self.context.variables, {"token": "T1"})
        self.assertEqual(self.context.headers["Content-Type"], "application/json; charset=utf-8")

    def test_body_json_parsed_from_text(self) -> None:
        self.assertEqual(self.context.body_json, BODY)
        self.assertTrue(self.context.has_json_body())

    def test_body_json_kept_when_provided(self) -> None:
        response = make_response(body_text="ignored", body_json={"given": True})
        self.assertEqual(build_context(response).body_json, {"given": True})

    def test_invalid_json_body(self) -> None:
        context = build_context(make_response(body_text="<html>boom</html>"))
        self.assertIsNone(context.body_json)
        self.assertFalse(context.has_json_body())

    def test_empty_body(self) -> None:
        context = build_context(make_response(body_text=""))
        self.assertIsNone(context.body_json)

    def test_json_null_body_is_kept(self) -> None:
        context = build_context(make_response(body_text="null"))
        self.assertIsNone(context.body_json)
        self.assertEqual(evaluate(spec(type="json_path", operator="eq", expected=None, path="$"), context).passed, True)

    def test_cookies_from_set_cookie_and_request_preview(self) -> None:
        self.assertEqual(self.context.cookies, {"sid": "abc123", "pre": "1"})

    def test_cookies_ignore_attributes(self) -> None:
        response = make_response(headers={"Set-Cookie": "a=1; Path=/; HttpOnly; Max-Age=60"}, request_preview={})
        self.assertEqual(build_context(response).cookies, {"a": "1"})

    def test_header_and_cookie_helpers_are_case_insensitive(self) -> None:
        self.assertEqual(self.context.header("content-TYPE"), "application/json; charset=utf-8")
        self.assertEqual(self.context.cookie("SID"), "abc123")
        self.assertIsNone(self.context.header("nope"))
        self.assertEqual(self.context.header("nope", "d"), "d")
        self.assertEqual(self.context.variable("token"), "T1")
        self.assertEqual(self.context.variable("nope", "d"), "d")

    def test_to_dict(self) -> None:
        data = self.context.to_dict()
        self.assertEqual(data["status"], 200)
        self.assertEqual(data["body_json"], BODY)
        self.assertEqual(data["cookies"], {"sid": "abc123", "pre": "1"})

    def test_none_response(self) -> None:
        context = build_context(None)  # type: ignore[arg-type]
        self.assertIsNone(context.status)
        self.assertEqual(context.body_text, "")
        self.assertEqual(context.variables, {})


class StatusCodeAssertionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.context = build_context(make_response())

    def test_pass_full_result(self) -> None:
        result = evaluate(spec(type="status_code", operator="eq", expected=200), self.context)
        self.assertEqual(
            result.to_dict(),
            {
                "index": 0,
                "type": "status_code",
                "operator": "eq",
                "target": "status_code",
                "expected": 200,
                "actual": 200,
                "passed": True,
                "message": "实际值 200 等于期望值 200",
                "describe": "status_code eq 200",
            },
        )

    def test_in_operator(self) -> None:
        result = evaluate(spec(type="status_code", operator="in", expected=[200, 201, 204]), self.context, index=3)
        self.assertTrue(result.passed)
        self.assertEqual(result.index, 3)
        self.assertEqual(result.message, "实际值 200 在期望集合 [200, 201, 204] 中")

    def test_failure(self) -> None:
        result = evaluate(spec(type="status_code", operator="eq", expected=500), self.context)
        self.assertFalse(result.passed)
        self.assertEqual(result.message, "实际值 200 不等于期望值 500")
        self.assertEqual(result.actual, 200)

    def test_missing_status(self) -> None:
        result = evaluate(spec(type="status_code", operator="eq", expected=200), build_context(make_response(status=None)))
        self.assertFalse(result.passed)
        self.assertEqual(result.message, "响应中没有状态码（请求可能未完成）")
        self.assertIsNone(result.actual)


class BodyTextAssertionTest(unittest.TestCase):
    def test_contains(self) -> None:
        context = build_context(make_response())
        result = evaluate(spec(type="body_text", operator="contains", expected='"msg": "ok"'), context)
        self.assertTrue(result.passed)
        self.assertEqual(result.target, "body_text")
        self.assertEqual(result.actual, json.dumps(BODY, ensure_ascii=False))

    def test_works_when_body_is_not_json(self) -> None:
        context = build_context(make_response(body_text="<html>boom</html>"))
        self.assertTrue(evaluate(spec(type="body_text", operator="contains", expected="boom"), context).passed)

    def test_regex(self) -> None:
        context = build_context(make_response(body_text="order=AB-12345"))
        result = evaluate(spec(type="body_text", operator="regex", expected=r"AB-\d+"), context)
        self.assertTrue(result.passed)
        self.assertEqual(result.message, '实际值 "order=AB-12345" 匹配正则 "AB-\\\\d+"')


class JsonPathAssertionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.context = build_context(make_response())

    def test_number_value(self) -> None:
        result = evaluate(spec(type="json_path", operator="eq", expected=42, path="$.data.id"), self.context)
        self.assertTrue(result.passed)
        self.assertEqual(result.target, "$.data.id")
        self.assertEqual(result.actual, 42)
        self.assertEqual(result.describe, "json_path[$.data.id] eq 42")

    def test_string_value(self) -> None:
        result = evaluate(spec(type="json_path", operator="eq", expected="hapitest", path="$.data.name"), self.context)
        self.assertTrue(result.passed)
        self.assertEqual(result.message, '实际值 "hapitest" 等于期望值 "hapitest"')

    def test_empty_path_treated_as_root_with_note(self) -> None:
        result = evaluate(spec(type="json_path", operator="type_is", expected="object", path=""), self.context)
        self.assertTrue(result.passed)
        self.assertEqual(result.target, "$")
        self.assertTrue(result.message.startswith('实际值 {"code": 0, "msg": "ok", "data": '), result.message)
        self.assertTrue(result.message.endswith("的类型为 object，符合期望 object；path 为空，已按 $ 处理"), result.message)

    def test_no_match(self) -> None:
        result = evaluate(spec(type="json_path", operator="eq", expected=1, path="$.data.nope"), self.context)
        self.assertFalse(result.passed)
        self.assertIsNone(result.actual)
        self.assertEqual(result.message, "路径无匹配: $.data.nope")

    def test_no_match_with_custom_message(self) -> None:
        result = evaluate(
            spec(type="json_path", operator="eq", expected=1, path="$.data.nope", message="业务字段缺失"),
            self.context,
        )
        self.assertEqual(result.message, "路径无匹配: $.data.nope；业务字段缺失")

    def test_multiple_matches_take_first_with_note(self) -> None:
        result = evaluate(spec(type="json_path", operator="eq", expected=1, path="$.data.list[*].id"), self.context)
        self.assertTrue(result.passed)
        self.assertEqual(
            result.message,
            "实际值 1 等于期望值 1；路径 $.data.list[*].id 命中 2 个结果，取第一个",
        )

    def test_exists_and_not_exists(self) -> None:
        self.assertTrue(evaluate(spec(type="json_path", operator="exists", path="$.data.id"), self.context).passed)
        exists_result = evaluate(spec(type="json_path", operator="exists", path="$.data.nope"), self.context)
        self.assertFalse(exists_result.passed)
        self.assertEqual(exists_result.message, "路径无匹配: $.data.nope")
        not_exists = evaluate(spec(type="json_path", operator="not_exists", path="$.data.nope"), self.context)
        self.assertTrue(not_exists.passed)
        self.assertEqual(not_exists.message, "路径无匹配: $.data.nope，符合 not_exists 期望")

    def test_exists_on_null_value_fails(self) -> None:
        context = build_context(make_response(body_text='{"a": null}'))
        result = evaluate(spec(type="json_path", operator="exists", path="$.a"), context)
        self.assertFalse(result.passed)
        self.assertEqual(result.message, "实际值为 null，期望存在")

    def test_not_exists_on_null_value_passes(self) -> None:
        context = build_context(make_response(body_text='{"a": null}'))
        result = evaluate(spec(type="json_path", operator="not_exists", path="$.a"), context)
        self.assertTrue(result.passed)
        self.assertEqual(result.message, "实际值为 null，符合不存在期望")

    def test_invalid_json_body(self) -> None:
        context = build_context(make_response(body_text="<html>"))
        result = evaluate(spec(type="json_path", operator="eq", expected=1, path="$.a"), context)
        self.assertFalse(result.passed)
        self.assertEqual(result.message, "响应体不是合法 JSON，无法执行 JSON 断言: $.a")

    def test_empty_body(self) -> None:
        context = build_context(make_response(body_text=""))
        result = evaluate(spec(type="json_path", operator="eq", expected=1, path="$.a"), context)
        self.assertEqual(result.message, "响应体为空，无法执行 JSON 断言: $.a")

    def test_invalid_expression(self) -> None:
        result = evaluate(spec(type="json_path", operator="eq", expected=1, path="$.a["), self.context)
        self.assertFalse(result.passed)
        self.assertTrue(result.message.startswith("JSONPath 表达式无效: JSONPath 语法错误"), result.message)

    def test_filter_expression(self) -> None:
        result = evaluate(spec(type="json_path", operator="eq", expected=2, path="$.data.list[?(@.status == 2)].id"), self.context)
        self.assertTrue(result.passed)
        self.assertEqual(result.actual, 2)


class HeaderCookieAssertionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.context = build_context(make_response())

    def test_header_case_insensitive(self) -> None:
        result = evaluate(spec(type="header", operator="contains", expected="application/json", name="content-type"), self.context)
        self.assertTrue(result.passed)
        self.assertEqual(result.target, "header:content-type")
        self.assertEqual(result.actual, "application/json; charset=utf-8")

    def test_header_exact_message(self) -> None:
        result = evaluate(spec(type="header", operator="eq", expected="application/xml", name="Content-Type"), self.context)
        self.assertFalse(result.passed)
        self.assertEqual(result.message, '实际值 "application/json; charset=utf-8" 不等于期望值 "application/xml"')

    def test_header_path_is_ignored(self) -> None:
        result = evaluate(
            spec(type="header", operator="eq", expected="t-1", name="x-trace-id", path="$.data.id"),
            self.context,
        )
        self.assertTrue(result.passed)
        self.assertEqual(result.target, "header:x-trace-id")

    def test_header_missing(self) -> None:
        result = evaluate(spec(type="header", operator="eq", expected="x", name="X-Nope"), self.context)
        self.assertFalse(result.passed)
        self.assertEqual(result.message, "响应头不存在: X-Nope")

    def test_header_missing_name(self) -> None:
        result = evaluate(spec(type="header", operator="eq", expected="x"), self.context)
        self.assertFalse(result.passed)
        self.assertEqual(result.message, "header 断言缺少 name（header 名称）")

    def test_cookie_case_insensitive(self) -> None:
        result = evaluate(spec(type="cookie", operator="eq", expected="abc123", name="SID"), self.context)
        self.assertTrue(result.passed)
        self.assertEqual(result.target, "cookie:SID")

    def test_cookie_from_request_preview(self) -> None:
        result = evaluate(spec(type="cookie", operator="eq", expected="1", name="pre"), self.context)
        self.assertTrue(result.passed)

    def test_cookie_missing(self) -> None:
        result = evaluate(spec(type="cookie", operator="eq", expected="x", name="nope"), self.context)
        self.assertFalse(result.passed)
        self.assertEqual(result.message, "响应 Cookie 不存在: nope")


class ResponseTimeAssertionTest(unittest.TestCase):
    def test_pass_and_target(self) -> None:
        context = build_context(make_response(duration_ms=123))
        result = evaluate(spec(type="response_time", operator="lt", expected=3000), context)
        self.assertTrue(result.passed)
        self.assertEqual(result.target, "duration_ms")
        self.assertEqual(result.message, "实际值 123 小于期望值 3000")

    def test_failure(self) -> None:
        context = build_context(make_response(duration_ms=5000))
        result = evaluate(spec(type="response_time", operator="lt", expected=3000), context)
        self.assertFalse(result.passed)
        self.assertEqual(result.message, "实际值 5000 不小于期望值 3000")


class VariableAssertionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.context = build_context(make_response(), {"token": "T1", "empty": None})

    def test_pass(self) -> None:
        result = evaluate(spec(type="variable", operator="eq", expected="T1", name="token"), self.context)
        self.assertTrue(result.passed)
        self.assertEqual(result.target, "variable:token")
        self.assertEqual(result.actual, "T1")

    def test_missing_variable(self) -> None:
        result = evaluate(spec(type="variable", operator="eq", expected="x", name="nope"), self.context)
        self.assertFalse(result.passed)
        self.assertEqual(result.message, "上下文中不存在变量 nope")

    def test_present_null_variable(self) -> None:
        result = evaluate(spec(type="variable", operator="is_empty", name="empty"), self.context)
        self.assertTrue(result.passed)
        self.assertIsNone(result.actual)

    def test_missing_name(self) -> None:
        result = evaluate(spec(type="variable", operator="eq", expected="x"), self.context)
        self.assertFalse(result.passed)
        self.assertEqual(result.message, "variable 断言缺少 name（变量名）")


class JsonSchemaAssertionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.context = build_context(make_response())

    def test_pass(self) -> None:
        schema = {"type": "object", "required": ["code"], "properties": {"code": {"type": "integer"}}}
        result = evaluate(spec(type="json_schema", operator="eq", expected=schema, path="$"), self.context)
        self.assertTrue(result.passed)
        self.assertEqual(result.message, "JSON Schema 校验通过")
        self.assertEqual(result.target, "$")

    def test_failure_lists_errors(self) -> None:
        schema = {"type": "object", "required": ["code", "missing"], "properties": {"code": {"type": "string"}}}
        result = evaluate(spec(type="json_schema", operator="eq", expected=schema, path="$"), self.context)
        self.assertFalse(result.passed)
        self.assertEqual(
            result.message,
            'JSON Schema 校验失败：$: 缺少必填字段 "missing"；$.code: 类型应为 string，实际为 integer（取值 0）',
        )

    def test_sub_path(self) -> None:
        schema = {"type": "object", "required": ["id"]}
        result = evaluate(spec(type="json_schema", operator="eq", expected=schema, path="$.data"), self.context)
        self.assertTrue(result.passed)

    def test_missing_expected(self) -> None:
        result = evaluate(spec(type="json_schema", operator="eq", path="$"), self.context)
        self.assertFalse(result.passed)
        self.assertEqual(result.message, "json_schema 断言缺少 expected（JSON Schema 定义）")

    def test_invalid_schema_type(self) -> None:
        result = evaluate(spec(type="json_schema", operator="eq", expected="nope", path="$"), self.context)
        self.assertFalse(result.passed)
        self.assertEqual(result.message, "JSON Schema 必须是对象或布尔值，实际为 str")

    def test_invalid_json_body(self) -> None:
        context = build_context(make_response(body_text="boom"))
        result = evaluate(spec(type="json_schema", operator="eq", expected={"type": "object"}, path="$"), context)
        self.assertFalse(result.passed)
        self.assertEqual(result.message, "响应体不是合法 JSON，无法执行 JSON 断言: $")

    def test_no_match_path(self) -> None:
        result = evaluate(spec(type="json_schema", operator="eq", expected={"type": "object"}, path="$.nope"), self.context)
        self.assertFalse(result.passed)
        self.assertEqual(result.message, "路径无匹配: $.nope")

    def test_operator_is_ignored(self) -> None:
        schema = {"type": "object", "required": ["code"]}
        result = evaluate(spec(type="json_schema", operator="not_exists", expected=schema, path="$"), self.context)
        self.assertTrue(result.passed)


class UnknownAssertionTest(unittest.TestCase):
    def test_unknown_type(self) -> None:
        result = evaluate(spec(type="wat", operator="eq", expected=1), build_context(make_response()))
        self.assertFalse(result.passed)
        self.assertEqual(result.message, "未知断言类型: wat")


class CompareOperatorTest(unittest.TestCase):
    def assert_compare(self, actual: Any, operator: str, expected: Any, passed: bool, message: str, tolerance: Any = None) -> None:
        got_passed, got_message = compare(actual, operator, expected, tolerance)
        self.assertEqual((got_passed, got_message), (passed, message))

    def test_eq(self) -> None:
        self.assert_compare(200, "eq", 200, True, "实际值 200 等于期望值 200")
        self.assert_compare(200, "eq", 201, False, "实际值 200 不等于期望值 201")
        self.assert_compare("abc", "eq", "abc", True, '实际值 "abc" 等于期望值 "abc"')
        self.assert_compare("10", "eq", 10, True, '实际值 "10" 等于期望值 10')
        self.assert_compare(10, "eq", "10.0", True, '实际值 10 等于期望值 "10.0"')
        self.assert_compare(True, "eq", "true", True, '实际值 true 等于期望值 "true"')
        self.assert_compare(False, "eq", "FALSE", True, '实际值 false 等于期望值 "FALSE"')
        self.assert_compare(True, "eq", 1, False, "实际值 true 不等于期望值 1")
        self.assert_compare(None, "eq", None, True, "实际值 null 等于期望值 null")
        self.assert_compare({"a": 1, "b": 2}, "eq", {"b": 2, "a": 1}, True, '实际值 {"a": 1, "b": 2} 等于期望值 {"b": 2, "a": 1}')

    def test_ne(self) -> None:
        self.assert_compare(200, "ne", 201, True, "实际值 200 不等于期望值 201")
        self.assert_compare(200, "ne", 200, False, "实际值 200 等于期望值 200")
        self.assert_compare(True, "ne", "false", True, '实际值 true 不等于期望值 "false"')

    def test_order_operators(self) -> None:
        self.assert_compare(10, "gt", 9, True, "实际值 10 大于期望值 9")
        self.assert_compare(9, "gt", 9, False, "实际值 9 不大于期望值 9")
        self.assert_compare("10", "gt", "9", True, '实际值 "10" 大于期望值 "9"')
        self.assert_compare("abc", "gt", "abd", False, '实际值 "abc" 不大于期望值 "abd"')
        self.assert_compare(9, "gte", 9, True, "实际值 9 大于等于期望值 9")
        self.assert_compare(8, "gte", 9, False, "实际值 8 小于期望值 9")
        self.assert_compare(8, "lt", 9, True, "实际值 8 小于期望值 9")
        self.assert_compare(9, "lt", 9, False, "实际值 9 不小于期望值 9")
        self.assert_compare(9, "lte", 9, True, "实际值 9 小于等于期望值 9")
        self.assert_compare(10, "lte", 9, False, "实际值 10 大于期望值 9")

    def test_order_with_null(self) -> None:
        self.assert_compare(None, "gt", 1, False, "无法比较：实际值 null 与期望值 1 中存在 null")

    def test_order_with_object(self) -> None:
        self.assert_compare({"a": 1}, "gt", 1, False, '操作符 gt 不支持对象/数组比较（实际值 {"a": 1}）')

    def test_contains(self) -> None:
        self.assert_compare([1, 2, 3], "contains", 2, True, "实际值 [1, 2, 3] 包含期望值 2（按元素判断）")
        self.assert_compare([1, 2, 3], "contains", 9, False, "实际值 [1, 2, 3] 不包含期望值 9（按元素判断）")
        self.assert_compare({"a": 1}, "contains", "a", True, '实际值 {"a": 1} 包含期望值 "a"（按键判断）')
        self.assert_compare("hello world", "contains", "world", True, '实际值 "hello world" 包含期望值 "world"（按子串判断）')
        self.assert_compare("hello", "contains", "zzz", False, '实际值 "hello" 不包含期望值 "zzz"（按子串判断）')
        self.assert_compare(None, "contains", "a", False, "实际值为 null，无法判断包含关系")

    def test_not_contains(self) -> None:
        self.assert_compare("hello", "not_contains", "zzz", True, '实际值 "hello" 不包含期望值 "zzz"（按子串判断）')
        self.assert_compare([1, 2], "not_contains", 1, False, "实际值 [1, 2] 包含期望值 1（按元素判断）")

    def test_starts_and_ends_with(self) -> None:
        self.assert_compare("hapitest", "starts_with", "hapi", True, '实际值 "hapitest" 以期望值 "hapi" 开头')
        self.assert_compare("hapitest", "starts_with", "xyz", False, '实际值 "hapitest" 不以期望值 "xyz" 开头')
        self.assert_compare("hapitest", "ends_with", "test", True, '实际值 "hapitest" 以期望值 "test" 结尾')
        self.assert_compare("hapitest", "ends_with", "xyz", False, '实际值 "hapitest" 不以期望值 "xyz" 结尾')
        self.assert_compare(None, "starts_with", "a", False, "实际值为 null，无法判断前缀")
        self.assert_compare(None, "ends_with", "a", False, "实际值为 null，无法判断后缀")

    def test_regex(self) -> None:
        self.assert_compare("abc123", "regex", "^abc[0-9]+$", True, '实际值 "abc123" 匹配正则 "^abc[0-9]+$"')
        self.assert_compare("abc", "regex", "^[0-9]+$", False, '实际值 "abc" 不匹配正则 "^[0-9]+$"')
        self.assert_compare(12345, "regex", "234", True, "实际值 12345 匹配正则 \"234\"")

    def test_regex_invalid_pattern(self) -> None:
        passed, message = compare("abc", "regex", "[")
        self.assertFalse(passed)
        self.assertTrue(message.startswith('正则表达式无效: "["'), message)

    def test_in_and_not_in(self) -> None:
        self.assert_compare(200, "in", [200, 201, 204], True, "实际值 200 在期望集合 [200, 201, 204] 中")
        self.assert_compare(500, "in", [200, 201, 204], False, "实际值 500 不在期望集合 [200, 201, 204] 中")
        self.assert_compare("b", "in", "abc", True, '实际值 "b" 在期望字符串 "abc" 中')
        self.assert_compare(500, "not_in", [200], True, "实际值 500 不在期望集合 [200] 中")
        self.assert_compare(200, "not_in", [200], False, "实际值 200 在期望集合 [200] 中")
        self.assert_compare(1, "in", 5, False, "操作符 in 的期望值必须是数组，实际为 5")

    def test_exists_and_not_exists(self) -> None:
        self.assert_compare(0, "exists", None, True, "实际值 0 存在")
        self.assert_compare("", "exists", None, True, '实际值 "" 存在')
        self.assert_compare(None, "exists", None, False, "实际值为 null，期望存在")
        self.assert_compare(None, "not_exists", None, True, "实际值为 null，符合不存在期望")
        self.assert_compare(0, "not_exists", None, False, "实际值 0 存在，期望不存在")

    def test_is_true_and_is_false(self) -> None:
        self.assert_compare(True, "is_true", None, True, "实际值 true 为真值")
        self.assert_compare("yes", "is_true", None, True, '实际值 "yes" 为真值')
        self.assert_compare(1, "is_true", None, True, "实际值 1 为真值")
        self.assert_compare(False, "is_true", None, False, "实际值 false 不是真值")
        self.assert_compare(False, "is_false", None, True, "实际值 false 为假值")
        self.assert_compare("no", "is_false", None, True, '实际值 "no" 为假值')
        self.assert_compare(True, "is_false", None, False, "实际值 true 不是假值")
        self.assert_compare("whatever", "is_true", None, False, '实际值 "whatever" 不是真值')

    def test_is_empty_and_not_empty(self) -> None:
        self.assert_compare(None, "is_empty", None, True, "实际值 null 为空")
        self.assert_compare("", "is_empty", None, True, '实际值 "" 为空')
        self.assert_compare([], "is_empty", None, True, "实际值 [] 为空")
        self.assert_compare({}, "is_empty", None, True, "实际值 {} 为空")
        self.assert_compare(0, "is_empty", None, False, "实际值 0 不为空，期望为空")
        self.assert_compare("x", "not_empty", None, True, '实际值 "x" 不为空')
        self.assert_compare([1], "not_empty", None, True, "实际值 [1] 不为空")
        self.assert_compare("", "not_empty", None, False, '实际值 "" 为空，期望不为空')

    def test_type_is(self) -> None:
        self.assert_compare("a", "type_is", "string", True, '实际值 "a" 的类型为 string，符合期望 string')
        self.assert_compare(1, "type_is", "number", True, "实际值 1 的类型为 integer，符合期望 number")
        self.assert_compare(1.5, "type_is", "number", True, "实际值 1.5 的类型为 number，符合期望 number")
        self.assert_compare(1, "type_is", "integer", True, "实际值 1 的类型为 integer，符合期望 integer")
        self.assert_compare(True, "type_is", "boolean", True, "实际值 true 的类型为 boolean，符合期望 boolean")
        self.assert_compare({}, "type_is", "object", True, "实际值 {} 的类型为 object，符合期望 object")
        self.assert_compare([], "type_is", "array", True, "实际值 [] 的类型为 array，符合期望 array")
        self.assert_compare(None, "type_is", "null", True, "实际值 null 的类型为 null，符合期望 null")
        self.assert_compare(True, "type_is", "integer", False, "实际值 true 的类型为 boolean，期望 integer")
        self.assert_compare(1.0, "type_is", "integer", False, "实际值 1.0 的类型为 number，期望 integer")
        self.assert_compare("a", "type_is", "wat", False, "type_is 的期望值必须是 string|number|integer|boolean|object|array|null 之一，实际为 \"wat\"")

    def test_length_operators(self) -> None:
        self.assert_compare("abc", "length_eq", 3, True, '实际值 "abc" 的长度 3 等于期望长度 3')
        self.assert_compare("abc", "length_eq", 2, False, '实际值 "abc" 的长度 3 不等于期望长度 2')
        self.assert_compare([1, 2], "length_gt", 1, True, "实际值 [1, 2] 的长度 2 大于期望长度 1")
        self.assert_compare([1, 2], "length_gt", 5, False, "实际值 [1, 2] 的长度 2 不大于期望长度 5")
        self.assert_compare({"a": 1}, "length_lt", 2, True, '实际值 {"a": 1} 的长度 1 小于期望长度 2')
        self.assert_compare({"a": 1}, "length_lt", 1, False, '实际值 {"a": 1} 的长度 1 不小于期望长度 1')
        self.assert_compare(5, "length_eq", 1, False, "操作符 length_eq 仅支持字符串/数组/对象，实际值 5 的类型为 integer")
        self.assert_compare("abc", "length_eq", "x", False, '操作符 length_eq 的期望值必须是数字，实际为 "x"')

    def test_between(self) -> None:
        self.assert_compare(5, "between", [1, 10], True, "实际值 5 落在区间 [1, 10] 内")
        self.assert_compare(11, "between", [1, 10], False, "实际值 11 不在区间 [1, 10] 内")
        self.assert_compare("b", "between", ["a", "c"], True, '实际值 "b" 落在区间 ["a", "c"] 内')
        self.assert_compare(5, "between", 1, False, "操作符 between 的期望值必须是 [min, max]，实际为 1")
        self.assert_compare(True, "between", [0, 1], False, "操作符 between 需要可比较的数值或字符串，实际为 true")

    def test_approx(self) -> None:
        self.assert_compare(100.0, "approx", 100.0000001, True, "实际值 100.0 约等于期望值 100.0000001（相对容差 1e-06）")
        self.assert_compare(1.0, "approx", 2.0, False, "实际值 1.0 与期望值 2.0 相差 1.0，超出容差 2e-06（相对容差 1e-06）")
        self.assert_compare(1.0, "approx", 1.1, True, "实际值 1.0 约等于期望值 1.1（相对容差 0.2）", tolerance=0.2)
        self.assert_compare(1.0, "approx", 1.1, False, "实际值 1.0 与期望值 1.1 相差 0.10000000000000009，超出容差 1.1e-06（相对容差 1e-06）")
        self.assert_compare(0.0, "approx", 0.0, True, "实际值 0.0 约等于期望值 0.0（相对容差 1e-06）")
        self.assert_compare(1e-9, "approx", 0.0, True, "实际值 1e-09 约等于期望值 0.0（相对容差 1e-06）")
        self.assert_compare("a", "approx", 1, False, '操作符 approx 需要数值，实际值 "a" / 期望值 1')

    def test_json_contains(self) -> None:
        actual = {"code": 0, "data": {"id": 1, "tags": ["a", "b"]}}
        self.assert_compare(
            actual,
            "json_contains",
            {"data": {"id": 1}},
            True,
            '实际值 {"code": 0, "data": {"id": 1, "tags": ["a", "b"]}} 包含期望子集 {"data": {"id": 1}}',
        )
        self.assert_compare(actual, "json_contains", {"data": {"id": 2}}, False,
                            '实际值 {"code": 0, "data": {"id": 1, "tags": ["a", "b"]}} 不包含期望子集 {"data": {"id": 2}}')
        self.assert_compare([{"id": 1, "x": 2}], "json_contains", [{"id": 1}], True,
                            '实际值 [{"id": 1, "x": 2}] 包含期望子集 [{"id": 1}]')
        self.assert_compare({"a": 1}, "json_contains", {}, True, '实际值 {"a": 1} 包含期望子集 {}')
        self.assert_compare([1, 2], "json_contains", [2], True, "实际值 [1, 2] 包含期望子集 [2]")
        self.assert_compare([1], "json_contains", {"a": 1}, False, '实际值 [1] 不包含期望子集 {"a": 1}')

    def test_json_equals(self) -> None:
        self.assert_compare(
            {"b": 2, "a": 1},
            "json_equals",
            {"a": 1, "b": 2},
            True,
            '实际值 {"b": 2, "a": 1} 与期望值 {"a": 1, "b": 2} 结构相等',
        )
        self.assert_compare([1, 2], "json_equals", [2, 1], False, "实际值 [1, 2] 与期望值 [2, 1] 结构不相等")
        self.assert_compare({"a": 1}, "json_equals", {"a": 1, "b": 2}, False,
                            '实际值 {"a": 1} 与期望值 {"a": 1, "b": 2} 结构不相等')
        self.assert_compare(1, "json_equals", 1.0, True, "实际值 1 与期望值 1.0 结构相等")
        self.assert_compare(True, "json_equals", 1, False, "实际值 true 与期望值 1 结构不相等")

    def test_unknown_operator(self) -> None:
        self.assert_compare(1, "wat", 1, False, '未知操作符: "wat"')

    def test_every_operator_is_implemented(self) -> None:
        from hapitest.models import OPERATORS

        for operator in OPERATORS:
            with self.subTest(operator=operator):
                passed, message = compare(1, operator, 1)
                self.assertIsInstance(passed, bool)
                self.assertNotEqual(message, "未知操作符: 1")
                self.assertFalse(message.startswith("未知操作符:"), message)


class EvaluateBehaviourTest(unittest.TestCase):
    def setUp(self) -> None:
        self.context = build_context(make_response())

    def test_never_raises_on_broken_context(self) -> None:
        class Exploding:
            def __getattr__(self, item: str) -> Any:
                raise RuntimeError("boom")

        result = evaluate(spec(type="status_code", operator="eq", expected=200), Exploding())  # type: ignore[arg-type]
        self.assertFalse(result.passed)
        self.assertEqual(result.message, "断言执行异常: RuntimeError: boom")

    def test_never_raises_on_bad_assertion(self) -> None:
        result = evaluate(123, self.context)  # type: ignore[arg-type]
        self.assertFalse(result.passed)
        self.assertEqual(result.message, "断言执行异常: TypeError: 断言必须是 AssertionSpec 或 dict，实际为 int")

    def test_dict_assertion_is_accepted(self) -> None:
        result = evaluate({"type": "status_code", "operator": "eq", "expected": 200}, self.context)  # type: ignore[arg-type]
        self.assertTrue(result.passed)
        self.assertEqual(result.type, "status_code")

    def test_disabled_assertion_is_skipped(self) -> None:
        result = evaluate(spec(type="status_code", operator="eq", expected=500, enabled=False), self.context)
        self.assertTrue(result.passed)
        self.assertEqual(result.message, "断言已禁用（enabled=false），跳过校验")
        self.assertEqual(result.target, "status_code")

    def test_custom_message_appended_on_failure(self) -> None:
        result = evaluate(spec(type="status_code", operator="eq", expected=500, message="业务异常"), self.context)
        self.assertEqual(result.message, "实际值 200 不等于期望值 500；业务异常")

    def test_custom_message_not_appended_on_pass(self) -> None:
        result = evaluate(spec(type="status_code", operator="eq", expected=200, message="业务异常"), self.context)
        self.assertEqual(result.message, "实际值 200 等于期望值 200")

    def test_evaluate_all_indices(self) -> None:
        assertions = [
            spec(type="status_code", operator="eq", expected=200),
            spec(type="status_code", operator="eq", expected=500),
            spec(type="response_time", operator="lt", expected=3000),
        ]
        results = evaluate_all(assertions, self.context)
        self.assertEqual([item.index for item in results], [0, 1, 2])
        self.assertEqual([item.passed for item in results], [True, False, True])

    def test_evaluate_all_empty(self) -> None:
        self.assertEqual(evaluate_all([], self.context), [])
        self.assertEqual(evaluate_all(None, self.context), [])  # type: ignore[arg-type]


class DefaultAssertionsTest(unittest.TestCase):
    def test_get_without_schema(self) -> None:
        self.assertEqual(
            summaries(default_assertions_for("GET")),
            [
                ("status_code", "in", [200, 201, 204], "", "", None, "", True),
                ("response_time", "lt", 3000, "", "", None, "", True),
            ],
        )

    def test_delete_accepts_different_statuses(self) -> None:
        self.assertEqual(
            summaries(default_assertions_for("delete")),
            [
                ("status_code", "in", [200, 202, 204], "", "", None, "", True),
                ("response_time", "lt", 3000, "", "", None, "", True),
            ],
        )

    def test_object_schema_with_code_data_and_required(self) -> None:
        schema = {
            "type": "object",
            "required": ["code", "data", "msg"],
            "properties": {"code": {"type": "integer"}, "data": {"type": "object"}, "msg": {"type": "string"}},
        }
        self.assertEqual(
            summaries(default_assertions_for("POST", schema)),
            [
                ("json_path", "eq", 0, "$.code", "", None, "", True),
                ("status_code", "in", [200, 201, 204], "", "", None, "", True),
                ("response_time", "lt", 3000, "", "", None, "", True),
                ("json_path", "exists", None, "$.data", "", None, "", True),
                ("json_path", "exists", None, "$.msg", "", None, "", True),
            ],
        )

    def test_required_properties_are_limited_to_five(self) -> None:
        schema = {"type": "object", "required": ["a", "b", "c", "d", "e", "f", "g"]}
        specs = default_assertions_for("GET", schema)
        self.assertEqual(
            [item.path for item in specs if item.type == "json_path"],
            ["$.a", "$.b", "$.c", "$.d", "$.e"],
        )

    def test_required_without_properties_still_generates(self) -> None:
        specs = default_assertions_for("GET", {"required": ["a"]})
        self.assertEqual([(item.type, item.path) for item in specs], [
            ("status_code", ""),
            ("response_time", ""),
            ("json_path", "$.a"),
        ])

    def test_weird_required_name_uses_bracket_path(self) -> None:
        specs = default_assertions_for("GET", {"type": "object", "required": ["a-b"]})
        self.assertEqual(specs[-1].path, "$['a-b']")

    def test_non_object_schema(self) -> None:
        specs = default_assertions_for("GET", {"type": "array", "items": {"type": "object"}})
        self.assertEqual(len(specs), 2)
        self.assertEqual(specs[0].type, "status_code")

    def test_type_list_with_object(self) -> None:
        schema = {"type": ["object", "null"], "properties": {"code": {"type": "integer"}}}
        specs = default_assertions_for("GET", schema)
        self.assertEqual(specs[0].path, "$.code")

    def test_deterministic(self) -> None:
        schema = {"type": "object", "required": ["code", "data"], "properties": {"code": {}, "data": {}}}
        self.assertEqual(summaries(default_assertions_for("GET", schema)), summaries(default_assertions_for("GET", schema)))

    def test_generated_assertions_pass_on_matching_response(self) -> None:
        schema = {
            "type": "object",
            "required": ["code", "data", "msg"],
            "properties": {"code": {"type": "integer"}, "data": {"type": "object"}},
        }
        context = build_context(make_response())
        results = evaluate_all(default_assertions_for("GET", schema), context)
        self.assertEqual([item.passed for item in results], [True] * len(results))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
