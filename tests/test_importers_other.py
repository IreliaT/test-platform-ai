"""Postman / Apifox / curl / Spring 扫描导入器测试（字段级断言 + 格式识别 + 分派）。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hapitest.importers import (
    ImportOptions,
    SOURCE_LABELS,
    detect_format,
    parse_apifox,
    parse_curl,
    parse_file,
    parse_postman,
    parse_text,
    scan_spring_project,
)
from hapitest.importers.postman import mine_assertions
from hapitest.models import validate_assertion

POSTMAN_JSON = """
{
  "info": {"name": "宠物集合", "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
  "variable": [{"key": "baseUrl", "value": "https://api.example.com"}, {"key": "apiKey", "value": "secret-value"}],
  "auth": {"type": "bearer", "bearer": [{"key": "token", "value": "{{token}}"}]},
  "header": [{"key": "Accept", "value": "application/json"}],
  "item": [
    {"name": "分组A", "item": [
      {"name": "获取宠物", "request": {
        "method": "GET",
        "url": {"raw": "{{baseUrl}}/pets/:petId?limit=5", "host": ["{{baseUrl}}"], "path": ["pets", ":petId"],
                "query": [{"key": "limit", "value": "5"}, {"key": "debug", "value": "1", "disabled": true}],
                "variable": [{"key": "petId", "value": "9"}]},
        "header": [{"key": "X-Trace", "value": "t-1"}],
        "description": "查询单个宠物"},
        "response": [{"code": 200, "body": "{\\"id\\": 9}"}],
        "event": [{"listen": "test", "script": {"exec": [
            "pm.test(\\"status\\", function () {",
            "  pm.response.to.have.status(200);",
            "});",
            "pm.expect(jsonData.data.id).to.eql(9);",
            "pm.expect(pm.response.json().name).to.equal('旺财');"
        ]}}]},
      {"name": "创建宠物", "request": {
        "method": "POST",
        "url": "https://api.example.com/pets",
        "header": [{"key": "Content-Type", "value": "application/json"}],
        "body": {"mode": "raw", "raw": "{\\"name\\": \\"旺财\\"}", "options": {"raw": {"language": "json"}}}},
        "event": [{"listen": "test", "script": {"exec": [
            "pm.expect(jsonData.code).to.include(0);",
            "pm.expect(pm.response.responseTime).to.be.below(200);"
        ]}}]}
    ]},
    {"name": "登录", "request": {
      "method": "POST", "url": "https://api.example.com/login",
      "auth": {"type": "basic", "basic": [{"key": "username", "value": "admin"}, {"key": "password", "value": "p@ss"}]},
      "body": {"mode": "urlencoded", "urlencoded": [{"key": "user", "value": "admin"}, {"key": "pwd", "value": "x"}]}}},
    {"name": "文件夹无请求", "item": [{"name": "空节点"}]}
  ]
}
"""

APIFOX_NATIVE_JSON = """
{
  "apiCollection": [
    {"name": "用户模块", "items": [
      {"name": "获取用户", "api": {
        "id": "api-1", "method": "get", "path": "/users/{id}", "description": "按 ID 查询",
        "parameters": {
          "path": [{"name": "id", "required": true, "schema": {"type": "integer", "example": 5}}],
          "query": [{"name": "verbose", "schema": {"type": "boolean", "default": true}}]},
        "responses": {"200": {"jsonSchema": {"type": "object", "required": ["id"],
                       "properties": {"id": {"type": "integer"}}}}}}},
      {"name": "创建用户", "api": {
        "method": "POST", "path": "/users",
        "requestBody": {"type": "application/json", "jsonSchema": {"type": "object", "required": ["name"],
          "properties": {"name": {"type": "string", "example": "张三"}, "age": {"type": "integer"},
                         "password": {"type": "string", "example": "secret"}}}},
        "responses": [{"code": 201, "jsonSchema": {"type": "object"}}]}}
    ]}
  ],
  "environments": [{"name": "测试环境", "baseUrl": "http://localhost:8080"}],
  "unknownTopKey": {"path": "/x"}
}
"""

SPRING_CONTROLLER = """
package com.example.user;

import com.example.common.ApiConstants;
import io.swagger.annotations.Api;
import io.swagger.annotations.ApiOperation;
import io.swagger.annotations.ApiParam;
import org.springframework.web.bind.annotation.*;

@RestController
@Api(value = "用户管理", tags = {"user"})
@RequestMapping(ApiConstants.API_PREFIX + "/users")
public class UserController {

    @GetMapping("/{id}")
    @ApiOperation(value = "查询用户", notes = "按 ID 查询用户详情")
    public UserDto getById(@ApiParam(value = "用户 ID", example = "1001") @PathVariable("id") Long id,
                           @RequestParam(value = "verbose", required = false, defaultValue = "false") boolean verbose,
                           @RequestHeader(value = "X-Trace", required = false) String trace) {
        return null;
    }

    @PostMapping(value = {"/", "/create"})
    @ApiOperation("创建用户")
    public UserDto create(@RequestBody UserDto body) {
        return null;
    }

    @RequestMapping(value = "/search", method = RequestMethod.POST, consumes = "application/x-www-form-urlencoded")
    public String search(@RequestParam String keyword, @RequestParam int page) {
        return null;
    }

    @DeleteMapping("/{id}")
    @Deprecated
    public void remove(@PathVariable Long id) {
    }
}
"""

SPRING_CONSTANTS = """
package com.example.common;

public final class ApiConstants {
    public static final String API_PREFIX = "/api/v1";
    public static final String USERS = API_PREFIX + "/users";
}
"""

SPRING_DTO = """
package com.example.user;

import io.swagger.annotations.ApiModelProperty;
import javax.validation.constraints.NotBlank;

public class UserDto {
    @ApiModelProperty(value = "用户 ID", example = "1001")
    private Long id;

    @ApiModelProperty(value = "用户名", required = true, example = "张三")
    @NotBlank
    private String name;

    @ApiModelProperty(value = "密码")
    private String password;

    private int age;

    private List<RoleDto> roles;
}
"""

APPLICATION_YML = """
server:
  port: 9090
  servlet:
    context-path: /gateway
spring:
  application:
    name: user-service
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class PostmanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.document = json.loads(POSTMAN_JSON)
        self.result = parse_postman(self.document, "pm.json")
        self.by_name = {item.name: item for item in self.result.endpoints}

    def test_metadata_and_folders(self) -> None:
        self.assertEqual("postman", self.result.source)
        self.assertEqual("宠物集合", self.result.title)
        self.assertEqual("pm.json", self.result.stats.source_ref)
        self.assertEqual(3, self.result.stats.created)
        self.assertEqual(1, self.result.stats.skipped)  # 缺失 request 的节点
        self.assertEqual("分组A", self.by_name["获取宠物"].folder)
        self.assertEqual("", self.by_name["登录"].folder)

    def test_url_placeholders_query_and_path_variables(self) -> None:
        endpoint = self.by_name["获取宠物"]
        self.assertEqual("GET", endpoint.method)
        self.assertEqual("{{baseUrl}}/pets/9", endpoint.path)
        self.assertIsNone(endpoint.request.base_url)
        self.assertEqual(
            [("limit", "5", True), ("debug", "1", False)],
            [(item.name, item.value, item.enabled) for item in endpoint.request.query],
        )
        locations = {item.name: item.location for item in endpoint.params}
        self.assertEqual({"petId": "path", "limit": "query", "debug": "query", "Accept": "header", "X-Trace": "header"}, locations)

    def test_inherited_headers_and_auth(self) -> None:
        endpoint = self.by_name["获取宠物"]
        headers = {item.name: item.value for item in endpoint.request.headers}
        self.assertEqual("application/json", headers["Accept"])
        self.assertEqual("t-1", headers["X-Trace"])
        self.assertEqual("bearer", endpoint.request.auth.type)
        self.assertEqual("{{token}}", endpoint.request.auth.token)

    def test_request_auth_overrides_collection_auth(self) -> None:
        endpoint = self.by_name["登录"]
        self.assertEqual("basic", endpoint.request.auth.type)
        self.assertEqual("admin", endpoint.request.auth.username)
        self.assertEqual("p@ss", endpoint.request.auth.password)
        self.assertEqual("form", endpoint.request.body_type)
        self.assertEqual({"user": "admin", "pwd": "x"}, endpoint.request.body)

    def test_json_raw_body(self) -> None:
        endpoint = self.by_name["创建宠物"]
        self.assertEqual("json", endpoint.request.body_type)
        self.assertEqual({"name": "旺财"}, endpoint.request.body)
        self.assertEqual("https://api.example.com", endpoint.request.base_url)
        self.assertEqual("/pets", endpoint.request.path)

    def test_collection_variables_are_reported_and_masked(self) -> None:
        self.assertIn("detected_variable: baseUrl", self.result.stats.warnings)
        self.assertIn("detected_variable: apiKey", self.result.stats.warnings)
        variables = self.result.cases[0].variables
        self.assertEqual("https://api.example.com", variables["baseUrl"])
        self.assertEqual("******", variables["apiKey"])

    def test_cases_with_mined_assertions(self) -> None:
        cases = {case.name: case for case in self.result.cases}
        self.assertEqual(3, self.result.stats.cases_created)
        first = cases["获取宠物 - 冒烟"]
        self.assertEqual("postman", first.source)
        self.assertEqual("pm.json", first.source_ref)
        self.assertEqual("P2", first.priority)
        self.assertEqual(
            [
                ("status_code", "eq", 200, ""),
                ("json_path", "eq", 9, "$.data.id"),
                ("json_path", "eq", "旺财", "$.name"),
            ],
            [(item.type, item.operator, item.expected, item.path) for item in first.assertions],
        )
        second = cases["创建宠物 - 冒烟"]
        self.assertEqual(
            [("json_path", "contains", 0, "$.code")],
            [(item.type, item.operator, item.expected, item.path) for item in second.assertions],
        )
        for case in self.result.cases:
            for assertion in case.assertions:
                self.assertEqual([], validate_assertion(assertion))

    def test_unsupported_script_warns(self) -> None:
        self.assertTrue(
            [item for item in self.result.stats.warnings if item.startswith("unsupported_script: pm.expect(pm.response.responseTime)")],
            msg=str(self.result.stats.warnings),
        )

    def test_mine_assertions_directly(self) -> None:
        events = [{"listen": "test", "script": {"exec": ["pm.response.to.have.status(404);"]}}]
        assertions = mine_assertions(events)
        self.assertEqual(1, len(assertions))
        self.assertEqual(404, assertions[0].expected)

    def test_saved_response_seeds_status_code(self) -> None:
        document = {
            "info": {"name": "c"},
            "item": [
                {
                    "name": "只有示例响应",
                    "request": {"method": "GET", "url": "http://example.com/health"},
                    "response": [{"code": 204}],
                }
            ],
        }
        result = parse_postman(document)
        case = result.cases[0]
        self.assertEqual([("status_code", 204)], [(item.type, item.expected) for item in case.assertions])

    def test_generate_cases_disabled(self) -> None:
        result = parse_postman(self.document, "pm.json", ImportOptions(generate_cases=False))
        self.assertEqual([], result.cases)
        self.assertEqual(0, result.stats.cases_created)


class ApifoxTest(unittest.TestCase):
    def test_openapi_export_is_delegated_and_relabelled(self) -> None:
        document = {
            "openapi": "3.0.0",
            "info": {"title": "Apifox 导出", "version": "1.0"},
            "paths": {"/a": {"get": {"summary": "A", "responses": {"200": {"description": "ok"}}}}},
        }
        result = parse_apifox(document, "apifox-openapi.json")
        self.assertEqual("apifox", result.source)
        self.assertEqual("apifox", result.stats.source)
        self.assertEqual("apifox", result.endpoints[0].source)
        self.assertEqual("apifox", result.cases[0].source)
        self.assertEqual("Apifox 导出", result.title)

    def test_native_export_walker(self) -> None:
        result = parse_apifox(json.loads(APIFOX_NATIVE_JSON), "apifox.json")
        self.assertEqual("apifox", result.source)
        self.assertEqual(2, result.stats.created)
        by_name = {item.name: item for item in result.endpoints}
        get_endpoint = by_name["获取用户"]
        self.assertEqual("GET", get_endpoint.method)
        self.assertEqual("/users/5", get_endpoint.request.path)
        self.assertEqual("用户模块", get_endpoint.folder)
        self.assertEqual("按 ID 查询", get_endpoint.description)
        self.assertEqual("api-1", get_endpoint.operation_id)
        self.assertEqual(
            [("id", "path", True, 5), ("verbose", "query", False, True)],
            [(item.name, item.location, item.required, item.example) for item in get_endpoint.params],
        )
        post_endpoint = by_name["创建用户"]
        self.assertEqual("json", post_endpoint.request.body_type)
        self.assertEqual({"name": "张三", "age": 0, "password": ""}, post_endpoint.request.body)
        self.assertIn("敏感字段已留空: password", result.stats.warnings)
        self.assertTrue([item for item in result.stats.warnings if item.startswith("未识别的顶层节点:")])

    def test_unknown_shape_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            parse_apifox({"foo": [1, 2, 3]})
        self.assertEqual("无法识别的 Apifox 导出格式", str(ctx.exception))
        with self.assertRaises(ValueError):
            parse_apifox("not a document")

    def test_skipped_node_records_warning(self) -> None:
        document = {"apis": [{"name": "坏节点", "path": "/x"}, {"name": "好节点", "path": "/y", "method": "get"}]}
        result = parse_apifox(document)
        self.assertEqual(1, result.stats.created)
        self.assertIn("跳过无法解析的接口节点: 坏节点", result.stats.warnings)


class CurlTest(unittest.TestCase):
    def test_full_command(self) -> None:
        command = (
            "curl -X POST 'https://api.example.com/v1/pets?debug=1' \\\n"
            "   -H 'Content-Type: application/json' \\\n"
            "   -H 'X-Trace: t-1' \\\n"
            "   -u 'admin:p@ss' \\\n"
            "   -k \\\n"
            "   -d '{\"name\": \"旺财\", \"age\": 3}'"
        )
        result = parse_curl(command)
        self.assertEqual("curl", result.source)
        self.assertEqual(1, result.stats.created)
        self.assertEqual(1, result.stats.cases_created)
        endpoint = result.endpoints[0]
        self.assertEqual("POST", endpoint.method)
        self.assertEqual("/v1/pets", endpoint.path)
        self.assertEqual("https://api.example.com", endpoint.request.base_url)
        self.assertEqual("json", endpoint.request.body_type)
        self.assertEqual({"name": "旺财", "age": 3}, endpoint.request.body)
        self.assertEqual([("debug", "1")], [(item.name, item.value) for item in endpoint.request.query])
        headers = {item.name: item.value for item in endpoint.request.headers}
        self.assertEqual("application/json", headers["Content-Type"])
        self.assertEqual("t-1", headers["X-Trace"])
        self.assertEqual("basic", endpoint.request.auth.type)
        self.assertEqual("admin", endpoint.request.auth.username)
        self.assertEqual("p@ss", endpoint.request.auth.password)
        self.assertFalse(endpoint.request.verify_tls)
        self.assertEqual(1, len(result.cases))
        self.assertEqual("POST /v1/pets - 冒烟", result.cases[0].name)

    def test_form_urlencode_and_cookies_and_get(self) -> None:
        result = parse_curl(
            "curl --compressed -L -b 'sid=abc; theme=dark' -d 'user=admin&pwd=x' http://localhost:8080/login"
        )
        request = result.endpoints[0].request
        self.assertEqual("form", request.body_type)
        self.assertEqual({"user": "admin", "pwd": "x"}, request.body)
        self.assertEqual([("sid", "abc"), ("theme", "dark")], [(item.name, item.value) for item in request.cookies])
        headers = {item.name: item.value for item in request.headers}
        self.assertEqual("gzip, deflate", headers["Accept-Encoding"])
        self.assertTrue(request.follow_redirects)

    def test_data_urlencode_with_get(self) -> None:
        result = parse_curl("curl -G --data-urlencode 'q=hello world' --data-urlencode 'page=2' https://a.example.com/search")
        endpoint = result.endpoints[0]
        self.assertEqual("GET", endpoint.method)
        self.assertEqual("none", endpoint.request.body_type)
        self.assertIsNone(endpoint.request.body)
        self.assertEqual(
            [("q", "hello world"), ("page", "2")],
            [(item.name, item.value) for item in endpoint.request.query],
        )

    def test_multipart_form(self) -> None:
        result = parse_curl("curl -F 'file=@/tmp/a.png' -F 'name=avatar' https://upload.example.com/api/files")
        request = result.endpoints[0].request
        self.assertEqual("multipart", request.body_type)
        self.assertEqual({"file": "@/tmp/a.png", "name": "avatar"}, request.body)
        self.assertIn("不支持表单字段上传文件: file=@/tmp/a.png", result.stats.warnings)

    def test_dollar_sign_is_preserved(self) -> None:
        result = parse_curl("curl -H 'Authorization: Bearer $TOKEN' https://api.example.com/me")
        headers = {item.name: item.value for item in result.endpoints[0].request.headers}
        self.assertEqual("Bearer $TOKEN", headers["Authorization"])

    def test_timeout_and_source_ref_masked(self) -> None:
        result = parse_curl("curl -m 3 'https://api.example.com/pets?token=abc123'")
        endpoint = result.endpoints[0]
        self.assertEqual(3000, endpoint.request.timeout_ms)
        # source_ref 取脱敏后的 URL：令牌不会落库
        self.assertNotIn("abc123", endpoint.source_ref)
        self.assertTrue(endpoint.source_ref.startswith("https://api.example.com/pets?token="), endpoint.source_ref)

    def test_invalid_command_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_curl("   ")

    def test_generate_cases_disabled(self) -> None:
        result = parse_curl("curl https://a.example.com/x", ImportOptions(generate_cases=False))
        self.assertEqual(1, result.stats.created)
        self.assertEqual([], result.cases)


class SpringScanTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hapitest-spring-")
        self.root = Path(self._tmp.name)
        _write(self.root / "src/main/java/com/example/common/ApiConstants.java", SPRING_CONSTANTS)
        _write(self.root / "src/main/java/com/example/user/UserDto.java", SPRING_DTO)
        _write(self.root / "src/main/java/com/example/user/UserController.java", SPRING_CONTROLLER)
        _write(self.root / "src/main/resources/application.yml", APPLICATION_YML)
        _write(self.root / "target/classes/Ignored.java", SPRING_CONTROLLER)
        _write(self.root / "src/main/java/com/example/user/Broken.java", "public class Broken { @GetMapping(\"/x\" \n")
        self.result = scan_spring_project(self.root)
        self.by_path = {"%s %s" % (item.method, item.path): item for item in self.result.endpoints}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_metadata_and_config_warnings(self) -> None:
        self.assertEqual("spring", self.result.source)
        self.assertEqual("user-service", self.result.title)
        self.assertEqual("spring", self.result.stats.source)
        self.assertIn("detected_application_name: user-service", self.result.stats.warnings)
        self.assertIn("detected_server_port: 9090", self.result.stats.warnings)
        self.assertIn("detected_context_path: /gateway", self.result.stats.warnings)
        self.assertEqual(4, self.result.stats.created)
        self.assertEqual(4, self.result.stats.cases_created)
        self.assertEqual(1, self.result.stats.skipped)

    def test_target_directory_is_skipped(self) -> None:
        refs = {item.source_ref for item in self.result.endpoints}
        self.assertFalse([ref for ref in refs if ref.startswith("target/")])

    def test_constant_and_context_path_resolution(self) -> None:
        endpoint = self.by_path["GET /gateway/api/v1/users/{id}"]
        self.assertEqual("查询用户", endpoint.name)
        self.assertEqual("user", endpoint.folder)
        self.assertEqual(["user"], endpoint.tags)
        self.assertEqual("getById", endpoint.operation_id)
        self.assertEqual("按 ID 查询用户详情", endpoint.description)
        self.assertEqual("/gateway/api/v1/users/1001", endpoint.request.path)
        self.assertEqual("src/main/java/com/example/user/UserController.java", endpoint.source_ref)
        self.assertFalse([item for item in self.result.stats.warnings if "未解析常量" in item])

    def test_path_query_header_params(self) -> None:
        endpoint = self.by_path["GET /gateway/api/v1/users/{id}"]
        params = {item.name: item for item in endpoint.params}
        self.assertEqual({"id", "verbose", "X-Trace"}, set(params))
        self.assertEqual("path", params["id"].location)
        self.assertEqual("integer", params["id"].type)
        self.assertTrue(params["id"].required)
        self.assertEqual("1001", params["id"].example)
        self.assertEqual("用户 ID", params["id"].description)
        self.assertEqual("query", params["verbose"].location)
        self.assertEqual("boolean", params["verbose"].type)
        self.assertEqual("false", params["verbose"].default)
        self.assertEqual("header", params["X-Trace"].location)
        self.assertEqual("false", endpoint.request.query[0].value)
        self.assertFalse(endpoint.request.query[0].enabled)

    def test_dto_body_and_body_params(self) -> None:
        endpoint = self.by_path["POST /gateway/api/v1/users"]
        self.assertEqual("json", endpoint.request.body_type)
        self.assertEqual(
            {"id": "1001", "name": "张三", "password": "", "age": 0, "roles": [{}]}, endpoint.request.body
        )
        body_params = {item.name: item for item in endpoint.params}
        self.assertEqual({"id", "name", "password", "age", "roles"}, set(body_params))
        self.assertTrue(body_params["name"].required)
        self.assertEqual("张三", body_params["name"].example)
        self.assertEqual("", body_params["password"].example)
        self.assertIn("敏感字段已留空: password", self.result.stats.warnings)
        self.assertTrue(
            [item for item in self.result.stats.warnings if "未找到类型 RoleDto" in item], msg=str(self.result.stats.warnings)
        )

    def test_multi_path_array_creates_two_endpoints(self) -> None:
        self.assertIn("POST /gateway/api/v1/users", self.by_path)
        self.assertIn("POST /gateway/api/v1/users/create", self.by_path)

    def test_request_mapping_with_consumes_form(self) -> None:
        endpoint = self.by_path["POST /gateway/api/v1/users/search"]
        self.assertEqual("form", endpoint.request.body_type)
        self.assertEqual({"keyword": "", "page": "0"}, endpoint.request.body)
        headers = {item.name: item.value for item in endpoint.request.headers}
        self.assertEqual("application/x-www-form-urlencoded", headers["Content-Type"])
        self.assertEqual([], endpoint.request.query)

    def test_deprecated_endpoint_skipped(self) -> None:
        self.assertNotIn("DELETE /gateway/api/v1/users/{id}", self.by_path)
        self.assertIn("已跳过过时接口: DELETE /gateway/api/v1/users/{id}", self.result.stats.warnings)

    def test_cases_and_assertions(self) -> None:
        self.assertEqual(4, len(self.result.cases))
        for case in self.result.cases:
            self.assertEqual("spring", case.source)
            self.assertEqual("P2", case.priority)
            self.assertTrue(case.assertions)

    def test_unparsable_file_does_not_crash(self) -> None:
        self.assertTrue(self.result.stats.created >= 4)

    def test_missing_root_raises(self) -> None:
        with self.assertRaises(ValueError):
            scan_spring_project(self.root / "not-exists")

    def test_broken_yaml_config_records_warning(self) -> None:
        _write(self.root / "src/main/resources/application-dev.yml", "server:\n  servlet:\n\tcontext-path: /x\n")
        result = scan_spring_project(self.root)
        self.assertTrue([item for item in result.stats.warnings if "配置文件解析失败" in item])

    def test_properties_config_is_read(self) -> None:
        _write(self.root / "src/main/resources/application-test.properties", "server.servlet.context-path=/prop\n")
        result = scan_spring_project(self.root)
        self.assertIn("detected_context_path: /prop", result.stats.warnings)


class FormatDetectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.openapi = {
            "openapi": "3.0.1",
            "info": {"title": "t", "version": "1"},
            "paths": {"/a": {"get": {"responses": {"200": {"description": "ok"}}}}},
        }
        self.swagger2 = {"swagger": "2.0", "info": {"title": "t", "version": "1"}, "paths": {}}
        self.postman = {"info": {"name": "c", "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"}, "item": []}
        self.apifox = {"apiCollection": [{"name": "目录", "apis": [{"name": "A", "path": "/a", "method": "get"}]}],
                       "environments": []}

    def test_detect_format_for_every_source(self) -> None:
        self.assertEqual("openapi", detect_format(json.dumps(self.openapi)))
        self.assertEqual("openapi", detect_format("openapi: 3.0.1\ninfo:\n  title: t\n"))
        self.assertEqual("swagger2", detect_format(json.dumps(self.swagger2)))
        self.assertEqual("postman", detect_format(json.dumps(self.postman)))
        self.assertEqual("apifox", detect_format(json.dumps(self.apifox)))
        self.assertEqual("curl", detect_format("curl -X GET https://a.example.com/x"))
        self.assertEqual("curl", detect_format("$ curl https://a.example.com/x"))
        self.assertEqual("spring", detect_format(SPRING_CONTROLLER))
        self.assertEqual("unknown", detect_format("hello: world\n"))
        self.assertEqual("unknown", detect_format(""))

    def test_parse_text_dispatch(self) -> None:
        self.assertEqual("openapi", parse_text(json.dumps(self.openapi), "a.json").source)
        self.assertEqual("postman", parse_text(json.dumps(self.postman), "c.json").source)
        self.assertEqual("apifox", parse_text(json.dumps(self.apifox), "af.json").source)
        self.assertEqual("curl", parse_text("curl https://a.example.com/x", "cmd.txt").source)

    def test_parse_text_errors(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            parse_text("hello: world", "x.txt")
        self.assertIn("无法识别导入格式", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            parse_text(SPRING_CONTROLLER, "UserController.java")
        self.assertIn("Spring 源码", str(ctx.exception))

    def test_parse_file_json_yaml_and_java(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hapitest-file-") as tmp:
            root = Path(tmp)
            json_path = root / "spec.json"
            json_path.write_text(json.dumps(self.openapi), encoding="utf-8")
            self.assertEqual("openapi", parse_file(json_path).source)

            yaml_path = root / "spec.yaml"
            yaml_path.write_text(
                "\ufeffswagger: '2.0'\ninfo:\n  title: 旧接口\n  version: '1'\npaths:\n  /a:\n    get:\n      responses:\n        200:\n          description: ok\n",
                encoding="utf-8",
            )
            result = parse_file(yaml_path)
            self.assertEqual("swagger2", result.source)
            self.assertEqual("旧接口", result.title)

            java_path = root / "Demo.java"
            java_path.write_text(SPRING_CONTROLLER, encoding="utf-8")
            self.assertEqual("spring", parse_file(java_path).source)

    def test_source_labels_cover_all_formats(self) -> None:
        for key in ("openapi", "swagger2", "postman", "apifox", "curl", "spring"):
            self.assertIn(key, SOURCE_LABELS)
            self.assertTrue(SOURCE_LABELS[key])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
