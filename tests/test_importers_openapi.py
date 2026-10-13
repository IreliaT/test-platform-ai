"""OpenAPI / Swagger 2.0 导入器测试：字段级断言、$ref、allOf、循环引用、用例生成。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from hapitest.importers import ImportOptions, detect_format, parse_openapi
from hapitest.importers.openapi import fallback_assertions
from hapitest.importers.yaml_lite import load_yaml_lite
from hapitest.models import AssertionSpec, validate_assertion

DEMO_SPEC = Path(__file__).resolve().parent.parent / "examples" / "demo_specs" / "openapi-petstore.yaml"

OPENAPI_JSON = """
{
  "openapi": "3.0.3",
  "info": {"title": "Pet Store", "version": "1.2.3", "description": "宠物商店示例接口。"},
  "servers": [{"url": "https://api.example.com/v1", "description": "生产环境"}],
  "tags": [{"name": "pets", "description": "宠物相关接口"}],
  "paths": {
    "/pets/{petId}": {
      "parameters": [
        {"name": "petId", "in": "path", "required": true, "description": "宠物 ID",
         "schema": {"type": "integer", "format": "int64", "example": 7}},
        {"name": "X-Trace", "in": "header", "schema": {"type": "string", "default": "trace-1"}}
      ],
      "get": {
        "tags": ["pets"],
        "summary": "查询宠物",
        "operationId": "getPet",
        "parameters": [
          {"name": "verbose", "in": "query", "schema": {"type": "boolean", "default": true}},
          {"name": "X-Trace", "in": "header", "schema": {"type": "string", "default": "override"}}
        ],
        "responses": {"200": {"description": "成功",
          "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Pet"}}}}}
      }
    },
    "/pets": {
      "post": {
        "tags": ["pets"],
        "summary": "创建宠物",
        "requestBody": {"required": true, "content": {
          "application/xml": {"schema": {"type": "string"}},
          "application/json": {"schema": {"$ref": "#/components/schemas/NewPet"}}}},
        "responses": {"201": {"description": "已创建",
          "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Pet"}}}}}
      }
    },
    "/old": {"get": {"deprecated": true, "responses": {"200": {"description": "ok"}}}}
  },
  "components": {"schemas": {
    "Pet": {"type": "object", "required": ["id", "name"], "properties": {
      "id": {"type": "integer", "format": "int64", "example": 1},
      "name": {"type": "string"},
      "tag": {"type": "string"}}},
    "Base": {"type": "object", "required": ["kind", "createdAt"], "properties": {
      "kind": {"type": "string", "example": "dog"},
      "createdAt": {"type": "string", "format": "date-time", "readOnly": true}}},
    "NewPet": {"allOf": [
      {"$ref": "#/components/schemas/Base"},
      {"type": "object", "required": ["name", "password"], "properties": {
        "name": {"type": "string", "example": "汪汪"},
        "age": {"type": "integer", "default": 3},
        "nickname": {"type": "string"},
        "password": {"type": "string", "example": "s3cr3t"}}}]}
  }}
}
"""

SWAGGER2_JSON = """
{
  "swagger": "2.0",
  "info": {"title": "Legacy API", "version": "2.0.0"},
  "host": "legacy.example.com",
  "basePath": "/api",
  "schemes": ["https"],
  "consumes": ["application/json"],
  "produces": ["application/json"],
  "paths": {
    "/users/{id}": {
      "get": {
        "tags": ["user"],
        "summary": "获取用户",
        "parameters": [
          {"name": "id", "in": "path", "required": true, "type": "integer", "format": "int64"},
          {"name": "verbose", "in": "query", "type": "boolean"}
        ],
        "responses": {"200": {"description": "ok", "schema": {"$ref": "#/definitions/User"}}}
      },
      "put": {
        "parameters": [
          {"name": "id", "in": "path", "required": true, "type": "string"},
          {"name": "body", "in": "body", "required": true, "schema": {"$ref": "#/definitions/User"}}
        ],
        "responses": {"200": {"description": "ok"}}
      }
    },
    "/login": {
      "post": {
        "consumes": ["application/x-www-form-urlencoded"],
        "parameters": [
          {"name": "username", "in": "formData", "required": true, "type": "string"},
          {"name": "password", "in": "formData", "required": true, "type": "string"}
        ],
        "responses": {"200": {"description": "ok"}}
      }
    },
    "/old": {"get": {"deprecated": true, "responses": {"200": {"description": "ok"}}}}
  },
  "definitions": {
    "User": {"type": "object", "required": ["id"], "properties": {
      "id": {"type": "integer"}, "name": {"type": "string"}}}
  }
}
"""

CIRCULAR_JSON = """
{
  "openapi": "3.0.0",
  "info": {"title": "循环引用", "version": "1.0"},
  "paths": {
    "/node": {"post": {
      "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Node"}}}},
      "responses": {"200": {"description": "ok"}}}}
  },
  "components": {"schemas": {
    "Node": {"type": "object", "required": ["name", "child"], "properties": {
      "name": {"type": "string"},
      "child": {"$ref": "#/components/schemas/Node"}}}
  }}
}
"""

OPENAPI31_JSON = """
{
  "openapi": "3.1.0",
  "info": {"title": "3.1 特性", "version": "1.0"},
  "paths": {
    "/x": {"post": {
      "requestBody": {"content": {"application/json": {
        "schema": {"$ref": "#/$defs/Payload", "description": "引用带兄弟字段"}}}},
      "responses": {"200": {"description": "ok",
        "content": {"application/json": {"schema": {"type": ["object", "null"]}}}}}}}
  },
  "$defs": {"Payload": {"type": "object", "required": ["name", "age", "kind", "nick"], "properties": {
    "name": {"type": ["string", "null"]},
    "age": {"type": "integer", "examples": [42]},
    "kind": {"const": "fixed"},
    "nick": {"type": "string", "default": "nn"}}}}
}
"""


class OpenApiDocumentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = json.loads(OPENAPI_JSON)
        self.result = parse_openapi(self.doc, "spec.json")

    # -- 顶层
    def test_result_metadata(self) -> None:
        self.assertEqual("openapi", self.result.source)
        self.assertEqual("Pet Store", self.result.title)
        self.assertEqual("1.2.3", self.result.version)
        self.assertEqual("openapi", self.result.stats.source)
        self.assertEqual("spec.json", self.result.stats.source_ref)
        self.assertEqual(2, self.result.stats.created)
        self.assertEqual(2, self.result.stats.cases_created)
        self.assertEqual(1, self.result.stats.skipped)
        self.assertIn("detected_server_url: https://api.example.com/v1", self.result.stats.warnings)
        self.assertEqual(2, len(self.result.endpoints))

    # -- GET /pets/{petId}
    def test_get_endpoint_fields(self) -> None:
        endpoint = self.result.endpoints[0]
        self.assertEqual("查询宠物", endpoint.name)
        self.assertEqual("pets", endpoint.folder)
        self.assertEqual("GET", endpoint.method)
        self.assertEqual("/pets/{petId}", endpoint.path)
        self.assertEqual("查询宠物", endpoint.summary)
        self.assertEqual(["pets"], endpoint.tags)
        self.assertEqual("getPet", endpoint.operation_id)
        self.assertFalse(endpoint.deprecated)
        self.assertEqual("openapi", endpoint.source)
        self.assertEqual("spec.json", endpoint.source_ref)
        self.assertEqual("", endpoint.created_at)

    def test_get_request_template(self) -> None:
        request = self.result.endpoints[0].request
        self.assertEqual("GET", request.method)
        # 路径参数用示例值替换，接口模板仍保留 {petId}
        self.assertEqual("/pets/7", request.path)
        self.assertIsNone(request.base_url)
        self.assertEqual("none", request.body_type)
        self.assertIsNone(request.body)
        self.assertEqual([], request.cookies)
        self.assertEqual(1, len(request.query))
        query = request.query[0]
        self.assertEqual("verbose", query.name)
        self.assertEqual("true", query.value)
        # 契约里给了示例值 → 即使是可选参数也要带上，
        # 否则冒烟请求会因缺参数收到 400/500（真实项目里踩到过）
        self.assertTrue(query.enabled)
        headers = {item.name: item.value for item in request.headers}
        self.assertEqual("override", headers["X-Trace"])  # 操作级覆盖路径级

    def test_optional_param_without_example_stays_disabled(self) -> None:
        """既可选又没有任何示例值的参数才保持关闭，避免带一堆空参数。"""
        import json

        from hapitest.importers.openapi import parse_openapi

        document = {
            "openapi": "3.0.3",
            "info": {"title": "t", "version": "1"},
            "paths": {
                "/x": {
                    "get": {
                        "parameters": [{"name": "flag", "in": "query", "schema": {"type": "string"}}],
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        }
        result = parse_openapi(json.loads(json.dumps(document)))
        query = result.endpoints[0].request.query[0]
        self.assertEqual("flag", query.name)
        self.assertFalse(query.enabled)

    def test_secret_param_becomes_variable_placeholder(self) -> None:
        """敏感参数不复制凭据，改写成 ${变量} 占位并启用。"""
        from hapitest.importers.openapi import parse_openapi

        document = {
            "openapi": "3.0.3",
            "info": {"title": "t", "version": "1"},
            "paths": {
                "/x": {
                    "get": {
                        "parameters": [
                            {"name": "Platform-Auth", "in": "header", "example": "real-token", "schema": {"type": "string"}}
                        ],
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        }
        result = parse_openapi(document)
        header = [item for item in result.endpoints[0].request.headers if item.name == "Platform-Auth"][0]
        self.assertEqual("${Platform-Auth}", header.value)
        self.assertTrue(header.enabled)
        self.assertNotIn("real-token", str(result.endpoints[0].request.to_dict()))
        self.assertTrue(any("Platform-Auth" in warning for warning in result.stats.warnings))

    def test_get_params_merge_order(self) -> None:
        params = self.result.endpoints[0].params
        self.assertEqual(["petId", "X-Trace", "verbose"], [item.name for item in params])
        pet_id = params[0]
        self.assertEqual("path", pet_id.location)
        self.assertEqual("integer", pet_id.type)
        self.assertTrue(pet_id.required)
        self.assertEqual(7, pet_id.example)
        self.assertEqual("宠物 ID", pet_id.description)
        trace = params[1]
        self.assertEqual("header", trace.location)
        self.assertFalse(trace.required)
        self.assertEqual("override", trace.example)
        self.assertEqual("override", trace.default)
        verbose = params[2]
        self.assertEqual("query", verbose.location)
        self.assertEqual("boolean", verbose.type)
        self.assertEqual(True, verbose.example)

    # -- POST /pets
    def test_post_body_and_params(self) -> None:
        endpoint = self.result.endpoints[1]
        self.assertEqual("POST", endpoint.method)
        self.assertEqual("/pets", endpoint.path)
        self.assertEqual("创建宠物", endpoint.name)
        # 媒体类型优先级：application/json 胜过排在前面的 application/xml
        self.assertEqual("json", endpoint.request.body_type)
        # allOf 合并 Base + NewPet；required 字段齐全，readOnly 字段被剔除
        self.assertEqual({"kind": "dog", "name": "汪汪", "age": 3, "password": ""}, endpoint.request.body)
        self.assertEqual(
            ["kind", "name", "age", "nickname", "password"], [item.name for item in endpoint.params]
        )
        self.assertEqual(["body"] * 5, [item.location for item in endpoint.params])
        required = {item.name: item.required for item in endpoint.params}
        self.assertEqual({"kind": True, "name": True, "age": False, "nickname": False, "password": True}, required)
        self.assertNotIn("createdAt", [item.name for item in endpoint.params])

    def test_secret_field_is_blank_with_warning(self) -> None:
        endpoint = self.result.endpoints[1]
        self.assertEqual("", endpoint.request.body["password"])
        self.assertIn("敏感字段已留空: password", self.result.stats.warnings)

    # -- 用例
    def test_generated_cases(self) -> None:
        case = self.result.cases[0]
        self.assertEqual("查询宠物 - 冒烟", case.name)
        self.assertEqual("pets", case.folder)
        self.assertEqual("openapi", case.source)
        self.assertEqual("spec.json", case.source_ref)
        self.assertEqual("P2", case.priority)
        self.assertEqual(["pets"], case.tags)
        self.assertEqual({}, case.variables)
        self.assertTrue(case.enabled)
        self.assertEqual(self.result.endpoints[0].request.to_dict(), case.request.to_dict())

    def test_case_assertions_are_valid(self) -> None:
        self.assertTrue(self.result.cases)
        for case in self.result.cases:
            self.assertTrue(case.assertions, msg=case.name)
            for assertion in case.assertions:
                self.assertIsInstance(assertion, AssertionSpec)
                self.assertEqual([], validate_assertion(assertion), msg=str(assertion.to_dict()))
            self.assertIn("status_code", [item.type for item in case.assertions])

    def test_case_request_is_independent_copy(self) -> None:
        case = self.result.cases[0]
        case.request.query.clear()
        case.request.path = "/changed"
        self.assertEqual(1, len(self.result.endpoints[0].request.query))
        self.assertEqual("/pets/7", self.result.endpoints[0].request.path)

    def test_deterministic_output(self) -> None:
        again = parse_openapi(json.loads(OPENAPI_JSON), "spec.json")
        self.assertEqual(
            json.dumps(self.result.to_dict(), ensure_ascii=False, sort_keys=False),
            json.dumps(again.to_dict(), ensure_ascii=False, sort_keys=False),
        )

    # -- YAML 与 JSON 一致
    def test_yaml_fixture_matches_json_document(self) -> None:
        yaml_document = load_yaml_lite(DEMO_SPEC.read_text(encoding="utf-8"))
        from_yaml = parse_openapi(yaml_document, "spec.json")
        self.assertEqual(
            json.dumps(self.result.to_dict(), ensure_ascii=False, sort_keys=True),
            json.dumps(from_yaml.to_dict(), ensure_ascii=False, sort_keys=True),
        )
        self.assertEqual("openapi", detect_format(DEMO_SPEC.read_text(encoding="utf-8")))


class OptionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = json.loads(OPENAPI_JSON)

    def test_folder_prefix_case_suffix_and_tags(self) -> None:
        options = ImportOptions(folder_prefix="demo", case_suffix=" - smoke", tags=["imported"])
        result = parse_openapi(self.doc, "spec.json", options)
        self.assertEqual("demo/pets", result.endpoints[0].folder)
        self.assertEqual("查询宠物 - smoke", result.cases[0].name)
        self.assertEqual(["pets", "imported"], result.endpoints[0].tags)
        self.assertEqual(["pets", "imported"], result.cases[0].tags)

    def test_default_headers_applied(self) -> None:
        options = ImportOptions(default_headers=["X-Env: test", "Bad-Header"])
        result = parse_openapi(self.doc, "spec.json", options)
        for endpoint in result.endpoints:
            headers = {item.name: item.value for item in endpoint.request.headers}
            self.assertEqual("test", headers["X-Env"])
            self.assertNotIn("Bad-Header", headers)

    def test_generate_cases_disabled(self) -> None:
        result = parse_openapi(self.doc, "spec.json", ImportOptions(generate_cases=False))
        self.assertEqual([], result.cases)
        self.assertEqual(0, result.stats.cases_created)
        self.assertEqual(2, result.stats.created)

    def test_base_url_suppresses_detected_server_warning(self) -> None:
        result = parse_openapi(self.doc, "spec.json", ImportOptions(base_url="http://127.0.0.1:8080"))
        self.assertFalse([item for item in result.stats.warnings if item.startswith("detected_server_url:")])
        self.assertIsNone(result.endpoints[0].request.base_url)

    def test_server_url_option_suppresses_warning(self) -> None:
        result = parse_openapi(self.doc, "spec.json", ImportOptions(server_url="https://api.example.com/v1"))
        self.assertFalse([item for item in result.stats.warnings if item.startswith("detected_server_url:")])

    def test_include_deprecated(self) -> None:
        result = parse_openapi(self.doc, "spec.json", ImportOptions(include_deprecated=True))
        self.assertEqual(3, result.stats.created)
        self.assertEqual(0, result.stats.skipped)
        deprecated = [item for item in result.endpoints if item.deprecated]
        self.assertEqual(1, len(deprecated))
        self.assertEqual("GET /old", deprecated[0].name)
        self.assertEqual(3, result.stats.cases_created)


class RefAndSchemaTest(unittest.TestCase):
    def test_circular_ref_is_cut_with_warning(self) -> None:
        result = parse_openapi(json.loads(CIRCULAR_JSON), "circular.json")
        endpoint = result.endpoints[0]
        self.assertEqual({"name": "", "child": {}}, endpoint.request.body)
        self.assertTrue(
            [item for item in result.stats.warnings if "循环 $ref" in item], msg=str(result.stats.warnings)
        )

    def test_openapi31_features(self) -> None:
        result = parse_openapi(json.loads(OPENAPI31_JSON), "oas31.json")
        body = result.endpoints[0].request.body
        # type 数组取非 null、const、examples 列表、default、$defs 引用带兄弟字段
        self.assertEqual({"name": "", "age": 42, "kind": "fixed", "nick": "nn"}, body)

    def test_oneof_takes_first_branch(self) -> None:
        doc = {
            "openapi": "3.0.0",
            "info": {"title": "t", "version": "1"},
            "paths": {
                "/x": {
                    "post": {
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {"oneOf": [{"type": "string", "format": "email"}, {"type": "integer"}]}
                                }
                            }
                        },
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        }
        result = parse_openapi(doc, "oneof.json")
        self.assertEqual("user@example.com", result.endpoints[0].request.body)
        self.assertIn("oneOf 仅取第一个分支生成示例", result.stats.warnings)

    def test_unresolvable_ref_warns(self) -> None:
        doc = {
            "openapi": "3.0.0",
            "info": {"title": "t", "version": "1"},
            "paths": {"/x": {"get": {"responses": {"200": {"description": "ok",
                      "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Missing"}}}}}}}},
        }
        result = parse_openapi(doc, "missing.json")
        self.assertTrue([item for item in result.stats.warnings if "无法解析 $ref" in item])

    def test_raw_and_form_and_multipart_bodies(self) -> None:
        doc = {
            "openapi": "3.0.0",
            "info": {"title": "bodies", "version": "1"},
            "paths": {
                "/raw": {"post": {"requestBody": {"content": {"text/plain": {"schema": {"type": "string"}}}},
                                  "responses": {"200": {"description": "ok"}}}},
                "/form": {"post": {"requestBody": {"content": {"application/x-www-form-urlencoded": {"schema": {
                    "type": "object", "required": ["a"], "properties": {"a": {"type": "string", "example": "1"}}}}}},
                    "responses": {"200": {"description": "ok"}}}},
                "/file": {"post": {"requestBody": {"content": {"multipart/form-data": {"schema": {
                    "type": "object", "properties": {"file": {"type": "string", "format": "binary"}}}}}},
                    "responses": {"200": {"description": "ok"}}}},
            },
        }
        result = parse_openapi(doc, "bodies.json")
        raw, form, upload = result.endpoints
        self.assertEqual("raw", raw.request.body_type)
        self.assertEqual("", raw.request.body)
        self.assertEqual("text/plain", {h.name: h.value for h in raw.request.headers}["Content-Type"])
        self.assertEqual("form", form.request.body_type)
        self.assertEqual({"a": "1"}, form.request.body)
        self.assertEqual(
            "application/x-www-form-urlencoded", {h.name: h.value for h in form.request.headers}["Content-Type"]
        )
        self.assertEqual("multipart", upload.request.body_type)
        self.assertEqual({"file": ""}, upload.request.body)
        self.assertEqual("multipart/form-data", {h.name: h.value for h in upload.request.headers}["Content-Type"])

    def test_nested_ref_in_property_is_generated(self) -> None:
        doc = {
            "openapi": "3.0.0",
            "info": {"title": "nested", "version": "1"},
            "paths": {
                "/x": {
                    "post": {
                        "requestBody": {"content": {"application/json": {"schema": {
                            "type": "object", "required": ["owner"], "properties": {
                                "owner": {"$ref": "#/components/schemas/Owner"}}}}}},
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
            "components": {"schemas": {"Owner": {"type": "object", "required": ["id"], "properties": {
                "id": {"type": "integer", "example": 3}}}}},
        }
        result = parse_openapi(doc, "nested.json")
        self.assertEqual({"owner": {"id": 3}}, result.endpoints[0].request.body)


class Swagger2Test(unittest.TestCase):
    def setUp(self) -> None:
        self.result = parse_openapi(json.loads(SWAGGER2_JSON), "swagger.json")
        self.by_path = {"%s %s" % (item.method, item.path): item for item in self.result.endpoints}

    def test_metadata_and_server_url(self) -> None:
        self.assertEqual("swagger2", self.result.source)
        self.assertEqual("swagger2", self.result.stats.source)
        self.assertEqual("Legacy API", self.result.title)
        self.assertEqual("2.0.0", self.result.version)
        self.assertIn("detected_server_url: https://legacy.example.com/api", self.result.stats.warnings)
        self.assertEqual(1, self.result.stats.skipped)

    def test_query_and_path_params_inline_types(self) -> None:
        endpoint = self.by_path["GET /users/{id}"]
        self.assertEqual(["user"], endpoint.tags)
        self.assertEqual("user", endpoint.folder)
        self.assertEqual("/users/0", endpoint.request.path)
        params = {item.name: item for item in endpoint.params}
        self.assertEqual("path", params["id"].location)
        self.assertEqual("integer", params["id"].type)
        self.assertTrue(params["id"].required)
        self.assertEqual("query", params["verbose"].location)
        self.assertEqual("boolean", params["verbose"].type)
        self.assertEqual(False, params["verbose"].example)
        self.assertEqual("false", endpoint.request.query[0].value)

    def test_body_parameter(self) -> None:
        endpoint = self.by_path["PUT /users/{id}"]
        self.assertEqual("json", endpoint.request.body_type)
        self.assertEqual({"id": 0}, endpoint.request.body)
        self.assertEqual(["id", "name"], [item.name for item in endpoint.params if item.location == "body"])

    def test_form_data_parameters(self) -> None:
        endpoint = self.by_path["POST /login"]
        self.assertEqual("form", endpoint.request.body_type)
        self.assertEqual({"username": "", "password": ""}, endpoint.request.body)
        headers = {item.name: item.value for item in endpoint.request.headers}
        self.assertEqual("application/x-www-form-urlencoded", headers["Content-Type"])
        self.assertIn("敏感字段已留空: password", self.result.stats.warnings)

    def test_cases_created_for_each_endpoint(self) -> None:
        self.assertEqual(3, self.result.stats.cases_created)
        self.assertEqual(
            ["获取用户 - 冒烟", "PUT /users/{id} - 冒烟", "POST /login - 冒烟"],
            [case.name for case in self.result.cases],
        )


class FallbackAssertionTest(unittest.TestCase):
    def test_fallback_assertions_shape(self) -> None:
        assertions = fallback_assertions("GET")
        self.assertEqual(2, len(assertions))
        self.assertEqual("status_code", assertions[0].type)
        self.assertEqual("in", assertions[0].operator)
        self.assertEqual([200, 201, 204], assertions[0].expected)
        self.assertEqual("response_time", assertions[1].type)
        self.assertEqual("lt", assertions[1].operator)
        self.assertEqual(3000, assertions[1].expected)
        for assertion in assertions:
            self.assertEqual([], validate_assertion(assertion))

    def test_cases_always_have_assertions(self) -> None:
        result = parse_openapi(json.loads(OPENAPI_JSON), "spec.json")
        for case in result.cases:
            self.assertTrue(case.assertions)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
