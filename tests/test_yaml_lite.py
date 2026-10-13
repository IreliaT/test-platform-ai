"""yaml_lite 测试：覆盖块结构、流式集合、引号/转义、块标量、报错行号与真实 OpenAPI YAML。"""

from __future__ import annotations

import unittest
from pathlib import Path

from hapitest.importers.yaml_lite import (
    HAS_PYYAML,
    YamlLiteError,
    load_file,
    load_yaml,
    load_yaml_lite,
)

DEMO_SPEC = Path(__file__).resolve().parent.parent / "examples" / "demo_specs" / "openapi-petstore.yaml"


class BlockStructureTest(unittest.TestCase):
    def test_block_mapping_and_sequence(self) -> None:
        text = (
            "name: demo\n"
            "count: 3\n"
            "ratio: 0.5\n"
            "enabled: true\n"
            "missing: null\n"
            "items:\n"
            "  - one\n"
            "  - two\n"
            "nested:\n"
            "  child:\n"
            "    leaf: 1\n"
            "same_indent:\n"
            "- a\n"
            "- b\n"
        )
        data = load_yaml_lite(text)
        self.assertEqual("demo", data["name"])
        self.assertEqual(3, data["count"])
        self.assertAlmostEqual(0.5, data["ratio"])
        self.assertIs(True, data["enabled"])
        self.assertIsNone(data["missing"])
        self.assertEqual(["one", "two"], data["items"])
        self.assertEqual({"child": {"leaf": 1}}, data["nested"])
        # 块序列可以与其父键同缩进
        self.assertEqual(["a", "b"], data["same_indent"])

    def test_sequence_of_mappings_and_nested_sequence(self) -> None:
        text = (
            "servers:\n"
            "  - url: https://a.example.com\n"
            "    description: 生产\n"
            "  - url: http://localhost:8080\n"
            "matrix:\n"
            "  - - 1\n"
            "    - 2\n"
            "  - k: v\n"
        )
        data = load_yaml_lite(text)
        self.assertEqual(2, len(data["servers"]))
        self.assertEqual("https://a.example.com", data["servers"][0]["url"])
        self.assertEqual("生产", data["servers"][0]["description"])
        self.assertEqual("http://localhost:8080", data["servers"][1]["url"])
        self.assertEqual([[1, 2], {"k": "v"}], data["matrix"])

    def test_scalar_coercion_rules(self) -> None:
        text = (
            "a: 007\n"
            "b: 1e3\n"
            "c: '1.0'\n"
            "d: yes\n"
            "e: NO\n"
            "f: ~\n"
            "g: 0x1f\n"
            "h: 2024-01-01\n"
            "i: \"200\"\n"
        )
        data = load_yaml_lite(text)
        self.assertEqual(7, data["a"])
        self.assertAlmostEqual(1000.0, data["b"])
        self.assertEqual("1.0", data["c"])
        self.assertIs(True, data["d"])
        self.assertIs(False, data["e"])
        self.assertIsNone(data["f"])
        self.assertEqual(31, data["g"])
        # 日期不做隐式转换，保证导入结果可复现
        self.assertEqual("2024-01-01", data["h"])
        # 引号包裹的数字保持字符串
        self.assertEqual("200", data["i"])

    def test_multiline_plain_scalar_folds(self) -> None:
        data = load_yaml_lite("description: 这是\n  一段很长的\n  文本\nnext: 1\n")
        self.assertEqual("这是 一段很长的 文本", data["description"])
        self.assertEqual(1, data["next"])


class FlowCollectionTest(unittest.TestCase):
    def test_inline_flow(self) -> None:
        data = load_yaml_lite("required: [id, name]\nschema: {type: integer, format: int64, example: 7}\nempty: {}\n")
        self.assertEqual(["id", "name"], data["required"])
        self.assertEqual({"type": "integer", "format": "int64", "example": 7}, data["schema"])
        self.assertEqual({}, data["empty"])

    def test_flow_across_lines_and_nested(self) -> None:
        text = "a: [1,\n  2,\n  {x: [true, null]}]\nb: {k: 'v: w', n: 3}\n"
        data = load_yaml_lite(text)
        self.assertEqual([1, 2, {"x": [True, None]}], data["a"])
        self.assertEqual({"k": "v: w", "n": 3}, data["b"])

    def test_quoted_keys_and_flow_keys(self) -> None:
        data = load_yaml_lite('"200": ok\n\'a:b\': 1\nflow: {"k": 1}\n')
        self.assertEqual("ok", data["200"])
        self.assertEqual(1, data["a:b"])
        self.assertEqual({"k": 1}, data["flow"])


class QuotingTest(unittest.TestCase):
    def test_single_and_double_quotes(self) -> None:
        text = (
            "single: 'it''s ok'\n"
            'double: "line\\nbreak\\ttab"\n'
            "hash: 'value # not comment'\n"
            "unicode: \"\\u0041\\u00e9\"\n"
        )
        data = load_yaml_lite(text)
        self.assertEqual("it's ok", data["single"])
        self.assertEqual("line\nbreak\ttab", data["double"])
        self.assertEqual("value # not comment", data["hash"])
        self.assertEqual("Aé", data["unicode"])

    def test_quoted_scalar_across_lines(self) -> None:
        data = load_yaml_lite("a: 'one\n  two'\nb: 'one\n\n  two'\n")
        self.assertEqual("one two", data["a"])
        self.assertEqual("one\ntwo", data["b"])


class CommentAndDocumentTest(unittest.TestCase):
    def test_comments_and_document_markers(self) -> None:
        text = "# 开头注释\n---\na: 1 # 行尾注释\n# 中间注释\nb: 2\n...\n"
        data = load_yaml_lite(text)
        self.assertEqual({"a": 1, "b": 2}, data)

    def test_empty_documents(self) -> None:
        self.assertIsNone(load_yaml_lite(""))
        self.assertIsNone(load_yaml_lite("# 只有注释\n"))
        self.assertIsNone(load_yaml_lite("---\n"))

    def test_second_document_is_ignored(self) -> None:
        self.assertEqual({"a": 1}, load_yaml_lite("a: 1\n---\nb: 2\n"))


class BlockScalarTest(unittest.TestCase):
    def test_literal_and_chomping(self) -> None:
        text = "clip: |\n  x\n  y\nstrip: |-\n  x\n  y\nkeep: |+\n  x\n\n\nnext: 1\n"
        data = load_yaml_lite(text)
        self.assertEqual("x\ny\n", data["clip"])
        self.assertEqual("x\ny", data["strip"])
        self.assertEqual("x\n\n\n", data["keep"])
        self.assertEqual(1, data["next"])

    def test_literal_keeps_inner_blank_lines_and_indent(self) -> None:
        text = "text: |\n  第一行\n\n    缩进行\n  第三行\n"
        data = load_yaml_lite(text)
        self.assertEqual("第一行\n\n  缩进行\n第三行\n", data["text"])

    def test_folded_and_explicit_indent(self) -> None:
        text = "folded: >\n  a\n  b\n\n  c\nexplicit: |2\n    indented\n"
        data = load_yaml_lite(text)
        self.assertEqual("a b\nc\n", data["folded"])
        self.assertEqual("  indented\n", data["explicit"])

    def test_block_scalar_in_sequence(self) -> None:
        text = "items:\n  - |\n    line1\n    line2\n  - plain\n"
        self.assertEqual(["line1\nline2\n", "plain"], load_yaml_lite(text)["items"])


class ErrorTest(unittest.TestCase):
    def assert_error(self, text: str, expected_line: int) -> YamlLiteError:
        with self.assertRaises(YamlLiteError) as ctx:
            load_yaml_lite(text)
        self.assertEqual(expected_line, ctx.exception.line, msg=str(ctx.exception))
        return ctx.exception

    def test_anchor_alias_merge_and_tag_are_rejected(self) -> None:
        self.assert_error("a: &anchor\n  b: 1\n", 1)
        self.assert_error("a: *alias\n", 1)
        self.assert_error("c:\n  <<: *base\n", 2)
        self.assert_error("a: !!str 1\n", 1)
        self.assertIn("锚点", str(self.assert_error("a: &anchor 1\n", 1)))

    def test_malformed_input_reports_line(self) -> None:
        self.assert_error("a: 1\n  b: 2\n", 2)
        self.assert_error("a:\n\tb: 1\n", 2)
        self.assert_error("a: 'unclosed\n", 1)
        self.assert_error("a: [1, 2\n", 1)
        self.assert_error('a: "bad \\q escape"\n', 1)
        self.assert_error("? complex\n: value\n", 1)

    def test_error_is_value_error(self) -> None:
        with self.assertRaises(ValueError):
            load_yaml_lite("a: [1,\n")


class RealSpecTest(unittest.TestCase):
    """用真实的 springdoc OpenAPI YAML 验证内置解析器。"""

    def setUp(self) -> None:
        self.text = DEMO_SPEC.read_text(encoding="utf-8")
        self.data = load_yaml_lite(self.text)

    def test_fixture_exists_and_is_parsed(self) -> None:
        self.assertTrue(DEMO_SPEC.exists())
        self.assertEqual("3.0.1", self.data["openapi"])
        self.assertEqual("Pet Store", self.data["info"]["title"])
        self.assertIn("springdoc", self.data["info"]["description"])
        self.assertEqual("https://api.example.com/v1", self.data["servers"][0]["url"])

    def test_refs_and_composition(self) -> None:
        get_op = self.data["paths"]["/pets/{petId}"]["get"]
        self.assertEqual("getPet", get_op["operationId"])
        self.assertEqual(["pets"], get_op["tags"])
        self.assertEqual(
            {"$ref": "#/components/schemas/Pet"},
            get_op["responses"]["200"]["content"]["application/json"]["schema"],
        )
        new_pet = self.data["components"]["schemas"]["NewPet"]
        self.assertEqual("#/components/schemas/Base", new_pet["allOf"][0]["$ref"])
        self.assertEqual(["name", "password"], new_pet["allOf"][1]["required"])
        self.assertIs(True, self.data["paths"]["/old"]["get"]["deprecated"])

    def test_load_yaml_agrees_with_lite(self) -> None:
        # 无论环境里有没有 PyYAML，load_yaml 都应得到同样的结构
        self.assertEqual(self.data, load_yaml(self.text))

    def test_load_file_with_bom_and_crlf(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spec.yaml"
            path.write_bytes(b"\xef\xbb\xbfa: 1\r\nb:\r\n  - x\r\n")
            self.assertEqual({"a": 1, "b": ["x"]}, load_file(path))

    def test_pyyaml_availability_is_reported(self) -> None:
        self.assertIsInstance(HAS_PYYAML, bool)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
