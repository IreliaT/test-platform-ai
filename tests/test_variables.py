"""``hapitest.variables`` 的单元测试：占位符语法、分层解析、响应提取与脱敏约定。"""

from __future__ import annotations

import json
import unittest

from hapitest import redact
from hapitest.models import ExtractorSpec, HttpResponse
from hapitest.variables import (
    VAR_PATTERN,
    VariableResolver,
    apply_extractors,
    build_resolver,
    extract_value,
)

BODY = {"code": 0, "data": {"id": 42, "token": "TK-123", "items": [1, 2, 3]}}


def make_response(**overrides: object) -> HttpResponse:
    values = {
        "ok": True,
        "status": 201,
        "headers": {"Content-Type": "application/json; charset=utf-8", "Set-Cookie": "sid=abc123; Path=/"},
        "body_text": json.dumps(BODY),
        "duration_ms": 88,
        "url": "http://127.0.0.1/api/x",
        "request_preview": {"headers": {"Cookie": "pre=1"}},
    }
    values.update(overrides)
    return HttpResponse(**values)  # type: ignore[arg-type]


def names(text: str) -> list:
    return [match.group("name") for match in VAR_PATTERN.finditer(text)]


class VarPatternTest(unittest.TestCase):
    def test_dollar_form(self) -> None:
        self.assertEqual(names("a=${host}/b"), ["host"])

    def test_double_brace_form(self) -> None:
        self.assertEqual(names("a={{host}}/b"), ["host"])

    def test_default_group(self) -> None:
        match = VAR_PATTERN.search("${port:-8080}")
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.group("name"), "port")
        self.assertEqual(match.group("default"), "8080")
        self.assertIsNone(match.group("optional"))
        self.assertEqual(match.group(0), "${port:-8080}")

    def test_optional_group(self) -> None:
        match = VAR_PATTERN.search("${token?}")
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.group("name"), "token")
        self.assertEqual(match.group("optional"), "?")
        self.assertIsNone(match.group("default"))

    def test_name_characters(self) -> None:
        self.assertEqual(names("${a.b-c_1}"), ["a.b-c_1"])
        self.assertEqual(names("${_x}"), ["_x"])

    def test_multiple_placeholders(self) -> None:
        self.assertEqual(names("${a} {{b}} ${c:-1} ${d?}"), ["a", "b", "c", "d"])

    def test_invalid_forms_are_not_matched(self) -> None:
        self.assertEqual(names("${1a}"), [])
        self.assertEqual(names("${}"), [])
        self.assertEqual(names("{{a}"), [])
        self.assertEqual(names("$a}"), [])
        self.assertEqual(names("${a b}"), [])

    def test_pattern_is_usable_with_sub(self) -> None:
        rendered = VAR_PATTERN.sub(lambda match: "<%s>" % match.group("name"), "a=${x} b={{y}}")
        self.assertEqual(rendered, "a=<x> b=<y>")


class ResolverLayerTest(unittest.TestCase):
    def test_first_added_layer_wins(self) -> None:
        resolver = build_resolver({"host": "first"}, {"host": "second"})
        self.assertEqual(resolver.get("host"), "first")

    def test_add_layer_appends_lower_priority(self) -> None:
        resolver = VariableResolver()
        resolver.add_layer({"host": "first"})
        resolver.add_layer({"host": "second", "path": "/v2"})
        self.assertEqual((resolver.get("host"), resolver.get("path")), ("first", "/v2"))

    def test_set_writes_to_override_layer(self) -> None:
        resolver = build_resolver({"host": "first"})
        resolver.set("host", "override")
        self.assertEqual(resolver.get("host"), "override")
        self.assertEqual(resolver.as_dict()["host"], "override")

    def test_set_does_not_touch_caller_dict(self) -> None:
        values = {"host": "first"}
        resolver = build_resolver(values)
        resolver.set("host", "override")
        resolver.set("new", "x")
        self.assertEqual(values, {"host": "first"})

    def test_add_layer_copies_values(self) -> None:
        values = {"host": "first"}
        resolver = build_resolver(values)
        values["host"] = "changed"
        self.assertEqual(resolver.get("host"), "first")

    def test_get_default(self) -> None:
        resolver = build_resolver({"a": None})
        self.assertIsNone(resolver.get("a", "d"))
        self.assertEqual(resolver.get("missing", "d"), "d")

    def test_as_dict_merges_lowest_first(self) -> None:
        resolver = build_resolver({"host": "h", "only_top": 1}, {"host": "low", "only_low": 2})
        self.assertEqual(resolver.as_dict(), {"host": "h", "only_low": 2, "only_top": 1})

    def test_resolver_accepts_single_mapping(self) -> None:
        resolver = VariableResolver({"host": "h"})
        self.assertEqual(resolver.get("host"), "h")

    def test_add_layer_rejects_non_mapping(self) -> None:
        resolver = VariableResolver()
        with self.assertRaises(TypeError) as ctx:
            resolver.add_layer(["a"])  # type: ignore[arg-type]
        self.assertEqual(str(ctx.exception), "变量层必须是 Mapping，实际为 list")

    def test_add_layer_none_is_empty(self) -> None:
        resolver = VariableResolver()
        resolver.add_layer(None)  # type: ignore[arg-type]
        self.assertEqual(resolver.as_dict(), {})


class ResolveTextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.resolver = build_resolver(
            {"host": "api.example.com", "port": 8080, "empty": "", "zero": 0, "flag": False, "nested": {"a": 1}}
        )

    def test_simple(self) -> None:
        self.assertEqual(self.resolver.resolve_text("http://${host}:${port}/v1"), "http://api.example.com:8080/v1")

    def test_double_brace(self) -> None:
        self.assertEqual(self.resolver.resolve_text("{{host}}"), "api.example.com")

    def test_missing_keeps_original_and_records(self) -> None:
        self.assertEqual(self.resolver.resolve_text("/${missing}/x"), "/${missing}/x")
        self.assertEqual(self.resolver.unresolved_names(), ["missing"])
        self.assertEqual(self.resolver.missing_names(), ["missing"])

    def test_missing_names_are_deduplicated_in_order(self) -> None:
        self.resolver.resolve_text("${b} ${a} ${b}")
        self.assertEqual(self.resolver.unresolved_names(), ["b", "a"])

    def test_missing_names_returns_copy(self) -> None:
        self.resolver.resolve_text("${a}")
        first = self.resolver.missing_names()
        first.append("injected")
        self.assertEqual(self.resolver.unresolved_names(), ["a"])

    def test_fallback_used_when_missing(self) -> None:
        self.assertEqual(self.resolver.resolve_text("${missing:-/default}"), "/default")
        self.assertEqual(self.resolver.unresolved_names(), [])

    def test_fallback_used_when_empty(self) -> None:
        self.assertEqual(self.resolver.resolve_text("[${empty:-fallback}]"), "[fallback]")
        self.assertEqual(self.resolver.unresolved_names(), [])

    def test_fallback_not_used_when_present(self) -> None:
        self.assertEqual(self.resolver.resolve_text("${host:-other}"), "api.example.com")

    def test_empty_fallback_value(self) -> None:
        self.assertEqual(self.resolver.resolve_text("${missing:-}"), "")

    def test_optional_placeholder_kept_without_recording(self) -> None:
        self.assertEqual(self.resolver.resolve_text("${missing?}"), "${missing?}")
        self.assertEqual(self.resolver.unresolved_names(), [])

    def test_optional_placeholder_resolves_when_present(self) -> None:
        self.assertEqual(self.resolver.resolve_text("${host?}"), "api.example.com")

    def test_empty_value_becomes_empty_string(self) -> None:
        self.assertEqual(self.resolver.resolve_text("[${empty}]"), "[]")

    def test_zero_and_false_are_not_empty(self) -> None:
        self.assertEqual(self.resolver.resolve_text("${zero}/${flag}"), "0/false")

    def test_non_string_values_are_rendered(self) -> None:
        self.assertEqual(self.resolver.resolve_text("${nested}"), '{"a": 1}')

    def test_resolution_is_single_pass(self) -> None:
        resolver = build_resolver({"a": "${b}", "b": "value"})
        self.assertEqual(resolver.resolve_text("${a}"), "${b}")
        self.assertEqual(resolver.unresolved_names(), [])

    def test_value_containing_backslash_is_literal(self) -> None:
        resolver = build_resolver({"a": r"C:\1"})
        self.assertEqual(resolver.resolve_text("${a}"), r"C:\1")

    def test_none_and_non_string_input(self) -> None:
        self.assertEqual(self.resolver.resolve_text(None), "")  # type: ignore[arg-type]
        self.assertEqual(self.resolver.resolve_text(5), "5")  # type: ignore[arg-type]

    def test_resolve_text_always_returns_str(self) -> None:
        self.assertIsInstance(self.resolver.resolve_text("${nested}"), str)
        self.assertIsInstance(self.resolver.resolve_text("${zero}"), str)


class ResolveValueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.resolver = build_resolver(
            {"host": "h", "port": 8080, "obj": {"a": 1}, "list": [1, 2], "none": None, "empty": ""}
        )

    def test_exact_placeholder_returns_raw_type(self) -> None:
        self.assertEqual(self.resolver.resolve_value("${port}"), 8080)
        self.assertEqual(self.resolver.resolve_value("${obj}"), {"a": 1})
        self.assertEqual(self.resolver.resolve_value("${list}"), [1, 2])
        self.assertIsNone(self.resolver.resolve_value("${none}"))
        self.assertEqual(self.resolver.resolve_value("${empty}"), "")

    def test_mixed_text_returns_string(self) -> None:
        self.assertEqual(self.resolver.resolve_value("port=${port}"), "port=8080")

    def test_missing_exact_placeholder_returns_text(self) -> None:
        self.assertEqual(self.resolver.resolve_value("${nope}"), "${nope}")
        self.assertEqual(self.resolver.unresolved_names(), ["nope"])

    def test_optional_missing_placeholder_returns_text_without_recording(self) -> None:
        self.assertEqual(self.resolver.resolve_value("${nope?}"), "${nope?}")
        self.assertEqual(self.resolver.unresolved_names(), [])

    def test_exact_placeholder_with_fallback(self) -> None:
        self.assertEqual(self.resolver.resolve_value("${nope:-d}"), "d")
        self.assertEqual(self.resolver.resolve_value("${empty:-d}"), "d")

    def test_nested_structures(self) -> None:
        value = {"url": "http://${host}", "list": ["${port}", {"deep": "${host}"}], "num": 7}
        self.assertEqual(
            self.resolver.resolve_value(value),
            {"url": "http://h", "list": [8080, {"deep": "h"}], "num": 7},
        )

    def test_tuple_preserved(self) -> None:
        result = self.resolver.resolve_value(("${host}", 1))
        self.assertEqual(result, ("h", 1))
        self.assertIsInstance(result, tuple)

    def test_non_string_scalars_pass_through(self) -> None:
        self.assertEqual(self.resolver.resolve_value(5), 5)
        self.assertIsNone(self.resolver.resolve_value(None))
        self.assertIs(self.resolver.resolve_value(True), True)

    def test_nested_keys_are_not_resolved(self) -> None:
        self.assertEqual(self.resolver.resolve_value({"${host}": 1}), {"${host}": 1})

    def test_resolve_mapping(self) -> None:
        self.assertEqual(self.resolver.resolve_mapping({"a": "${host}", "b": 5}), {"a": "h", "b": 5})
        self.assertEqual(self.resolver.resolve_mapping(None), {})  # type: ignore[arg-type]


class ExtractValueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.response = make_response()

    def extract(self, source: str, expression: str = "", default: object = None) -> tuple:
        return extract_value(self.response, ExtractorSpec(name="v", source=source, expression=expression, default=default))

    def test_json_path_success(self) -> None:
        self.assertEqual(self.extract("json_path", "$.data.id"), (True, 42, "json_path $.data.id 提取成功"))

    def test_json_path_parses_body_text(self) -> None:
        response = make_response(body_json=None)
        self.assertEqual(
            extract_value(response, ExtractorSpec(name="v", source="json_path", expression="$.code")),
            (True, 0, "json_path $.code 提取成功"),
        )

    def test_json_path_empty_expression_returns_whole_body(self) -> None:
        self.assertEqual(self.extract("json_path", ""), (True, BODY, "json_path $ 提取成功"))

    def test_json_path_no_match(self) -> None:
        self.assertEqual(self.extract("json_path", "$.nope"), (False, None, "JSONPath 无匹配: $.nope"))

    def test_json_path_no_match_uses_default(self) -> None:
        self.assertEqual(self.extract("json_path", "$.nope", "D"), (False, "D", "JSONPath 无匹配: $.nope"))

    def test_json_path_invalid_body(self) -> None:
        response = make_response(body_text="<html>", body_json=None)
        self.assertEqual(
            extract_value(response, ExtractorSpec(name="v", source="json_path", expression="$.a")),
            (False, None, "响应体不是合法 JSON，无法按 json_path 提取"),
        )

    def test_json_path_empty_body(self) -> None:
        response = make_response(body_text="", body_json=None)
        self.assertEqual(
            extract_value(response, ExtractorSpec(name="v", source="json_path", expression="$.a")),
            (False, None, "响应体为空，无法按 json_path 提取"),
        )

    def test_json_path_invalid_expression(self) -> None:
        ok, value, message = self.extract("json_path", "$.a[")
        self.assertFalse(ok)
        self.assertIsNone(value)
        self.assertTrue(message.startswith("JSONPath 表达式无效: JSONPath 语法错误"), message)

    def test_header_case_insensitive(self) -> None:
        self.assertEqual(self.extract("header", "content-TYPE"), (True, "application/json; charset=utf-8", "响应头 content-TYPE 提取成功"))

    def test_header_missing(self) -> None:
        self.assertEqual(self.extract("header", "X-Nope"), (False, None, "响应头不存在: X-Nope"))

    def test_header_missing_expression(self) -> None:
        self.assertEqual(self.extract("header", ""), (False, None, "header 提取器缺少响应头名称"))

    def test_cookie_from_set_cookie(self) -> None:
        self.assertEqual(self.extract("cookie", "SID"), (True, "abc123", "Cookie SID 提取成功"))

    def test_cookie_from_request_preview(self) -> None:
        self.assertEqual(self.extract("cookie", "pre"), (True, "1", "Cookie pre 提取成功"))

    def test_cookie_missing(self) -> None:
        self.assertEqual(self.extract("cookie", "nope"), (False, None, "响应 Cookie 不存在: nope"))

    def test_cookie_missing_expression(self) -> None:
        self.assertEqual(self.extract("cookie", ""), (False, None, "cookie 提取器缺少 Cookie 名称"))

    def test_regex_first_group(self) -> None:
        response = make_response(body_text='{"code": 42}')
        self.assertEqual(
            extract_value(response, ExtractorSpec(name="v", source="regex", expression=r'"code":\s*(\d+)')),
            (True, "42", r'正则 "code":\s*(\d+) 提取成功'),
        )

    def test_regex_whole_match_without_group(self) -> None:
        response = make_response(body_text="order=AB-12345")
        self.assertEqual(
            extract_value(response, ExtractorSpec(name="v", source="regex", expression=r"AB-\d+")),
            (True, "AB-12345", r"正则 AB-\d+ 提取成功"),
        )

    def test_regex_no_match(self) -> None:
        self.assertEqual(self.extract("regex", "zzz"), (False, None, "正则未匹配: zzz"))

    def test_regex_invalid_pattern(self) -> None:
        ok, value, message = self.extract("regex", "(")
        self.assertFalse(ok)
        self.assertIsNone(value)
        self.assertTrue(message.startswith("正则表达式无效: ("), message)

    def test_regex_missing_expression(self) -> None:
        self.assertEqual(self.extract("regex", ""), (False, None, "regex 提取器缺少正则表达式"))

    def test_body_text(self) -> None:
        self.assertEqual(self.extract("body_text"), (True, json.dumps(BODY), "已提取响应体文本"))

    def test_status_code(self) -> None:
        self.assertEqual(self.extract("status_code"), (True, 201, "状态码提取成功"))

    def test_status_code_missing(self) -> None:
        response = make_response(status=None)
        self.assertEqual(
            extract_value(response, ExtractorSpec(name="v", source="status_code")),
            (False, None, "响应中没有状态码（请求可能未完成）"),
        )

    def test_duration_ms(self) -> None:
        self.assertEqual(self.extract("duration_ms"), (True, 88, "响应耗时提取成功"))

    def test_unknown_source(self) -> None:
        self.assertEqual(self.extract("nope"), (False, None, "未知提取来源: nope"))
        self.assertEqual(self.extract("nope", "", "D"), (False, "D", "未知提取来源: nope"))

    def test_never_raises(self) -> None:
        ok, value, message = extract_value(None, ExtractorSpec(name="v", source="json_path", expression="$.a"))  # type: ignore[arg-type]
        self.assertFalse(ok)
        self.assertIsNone(value)
        self.assertEqual(message, "响应体为空，无法按 json_path 提取")

    def test_missing_body_json_flag(self) -> None:
        response = make_response(body_json=None)
        self.assertEqual(
            extract_value(response, ExtractorSpec(name="v", source="json_path", expression="$.data.items[1]")),
            (True, 2, "json_path $.data.items[1] 提取成功"),
        )


class ApplyExtractorsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.response = make_response()
        self.resolver = build_resolver({"known": "K"})

    def test_success_writes_resolver_and_returns_values(self) -> None:
        extractors = [
            ExtractorSpec(name="id", source="json_path", expression="$.data.id"),
            ExtractorSpec(name="ctype", source="header", expression="Content-Type"),
        ]
        extracted, warnings = apply_extractors(self.response, extractors, self.resolver)
        self.assertEqual(extracted, {"id": 42, "ctype": "application/json; charset=utf-8"})
        self.assertEqual(warnings, [])
        self.assertEqual(self.resolver.get("id"), 42)
        self.assertEqual(self.resolver.resolve_text("${id}"), "42")

    def test_secret_stored_plain_but_returned_masked(self) -> None:
        extractors = [ExtractorSpec(name="access_token", source="json_path", expression="$.data.token")]
        extracted, warnings = apply_extractors(self.response, extractors, self.resolver)
        self.assertEqual(warnings, [])
        # resolver 里必须是明文（后续请求要用真实值）
        self.assertEqual(self.resolver.get("access_token"), "TK-123")
        # 返回给日志/历史的字典必须脱敏
        self.assertEqual(extracted, {"access_token": redact.MASK})
        self.assertTrue(redact.is_secret_key("access_token"))

    def test_failure_with_default(self) -> None:
        extractors = [ExtractorSpec(name="miss", source="json_path", expression="$.nope", default="D")]
        extracted, warnings = apply_extractors(self.response, extractors, self.resolver)
        self.assertEqual(extracted, {"miss": "D"})
        self.assertEqual(warnings, ["变量 miss 未提取到：JSONPath 无匹配: $.nope，使用默认值"])
        self.assertEqual(self.resolver.get("miss"), "D")

    def test_failure_without_default(self) -> None:
        extractors = [ExtractorSpec(name="miss", source="header", expression="X-Nope")]
        extracted, warnings = apply_extractors(self.response, extractors, self.resolver)
        self.assertEqual(extracted, {"miss": None})
        self.assertEqual(warnings, ["变量 miss 未提取到：响应头不存在: X-Nope"])
        self.assertNotIn("miss", self.resolver.as_dict())
        self.assertEqual(self.resolver.resolve_text("${miss}"), "${miss}")

    def test_required_failure_is_recorded(self) -> None:
        extractors = [ExtractorSpec(name="miss", source="json_path", expression="$.nope", required=True)]
        extracted, warnings = apply_extractors(self.response, extractors, self.resolver)
        self.assertEqual(extracted, {"miss": None})
        self.assertEqual(warnings, ["必填变量 miss 未提取到：JSONPath 无匹配: $.nope"])

    def test_required_failure_with_default(self) -> None:
        extractors = [
            ExtractorSpec(name="miss", source="json_path", expression="$.nope", required=True, default=0),
        ]
        extracted, warnings = apply_extractors(self.response, extractors, self.resolver)
        self.assertEqual(extracted, {"miss": 0})
        self.assertEqual(warnings, ["必填变量 miss 未提取到：JSONPath 无匹配: $.nope，使用默认值"])

    def test_none_value_is_not_masked_to_empty(self) -> None:
        extractors = [ExtractorSpec(name="token", source="json_path", expression="$.nope")]
        extracted, _warnings = apply_extractors(self.response, extractors, self.resolver)
        self.assertEqual(extracted, {"token": None})

    def test_empty_name_is_skipped(self) -> None:
        extractors = [ExtractorSpec(name="", source="body_text")]
        extracted, warnings = apply_extractors(self.response, extractors, self.resolver)
        self.assertEqual(extracted, {})
        self.assertEqual(warnings, ["提取器缺少变量名，已跳过"])

    def test_none_extractor_is_skipped(self) -> None:
        extracted, warnings = apply_extractors(self.response, [None], self.resolver)  # type: ignore[list-item]
        self.assertEqual(extracted, {})
        self.assertEqual(warnings, ["提取器缺少变量名，已跳过"])

    def test_extractor_exception_becomes_warning(self) -> None:
        class Boom:
            @property
            def name(self) -> str:
                raise RuntimeError("boom")

        extracted, warnings = apply_extractors(self.response, [Boom()], self.resolver)  # type: ignore[list-item]
        self.assertEqual(extracted, {})
        self.assertEqual(warnings, ["提取器执行异常: RuntimeError: boom"])

    def test_empty_extractors(self) -> None:
        self.assertEqual(apply_extractors(self.response, [], self.resolver), ({}, []))
        self.assertEqual(apply_extractors(self.response, None, self.resolver), ({}, []))  # type: ignore[arg-type]

    def test_warnings_keep_extractor_order(self) -> None:
        extractors = [
            ExtractorSpec(name="a", source="json_path", expression="$.nope"),
            ExtractorSpec(name="b", source="header", expression="X-Nope"),
        ]
        _extracted, warnings = apply_extractors(self.response, extractors, self.resolver)
        self.assertEqual(
            warnings,
            ["变量 a 未提取到：JSONPath 无匹配: $.nope", "变量 b 未提取到：响应头不存在: X-Nope"],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
