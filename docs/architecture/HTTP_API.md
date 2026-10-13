# HapiTest HTTP API（v1）

所有接口以 `/api/v1` 为前缀，返回 JSON。

## 响应信封

成功：

```json
{ "ok": true, "data": { } }
```

失败（HTTP 状态码同时反映错误类别）：

```json
{ "ok": false, "error": { "code": "not_found", "message": "项目不存在: foo" } }
```

| HTTP | code | 含义 |
|---|---|---|
| 400 | `bad_request` | 参数或请求体不合法（附 `details` 数组） |
| 404 | `not_found` | 资源不存在 |
| 405 | `method_not_allowed` | 方法不支持 |
| 409 | `conflict` | 状态冲突（如同 request_id 但参数不同、运行中不可删除） |
| 500 | `internal_error` | 平台内部错误 |

分页参数统一 `limit`（默认 50，上限 500）与 `offset`；列表响应形如 `{"items": [...], "total": 123, "limit": 50, "offset": 0}`。

静态资源：`GET /` 与任意非 `/api/` 路径返回 `hapitest/web/index.html`（前端使用 hash 路由）；`GET /assets/<file>` 返回 `hapitest/web/` 下的静态文件。

---

## 1. 平台级

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/health` | `{name, version, protocol, time, uptime_s}` |
| GET | `/api/v1/meta` | 枚举字典：`http_methods, body_types, auth_types, assertion_types, operators, priorities, case_statuses, run_statuses, import_sources, extractor_sources` |

## 2. 项目

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/projects` | 项目列表，每项含 `counts`（endpoint/case/suite/run 数量） |
| POST | `/api/v1/projects` | body `{id?, name, description?, base_url?}`；`id` 省略时由名称生成 |
| GET | `/api/v1/projects/{pid}` | `{project, counts}` |
| PUT | `/api/v1/projects/{pid}` | 更新 `name/description/base_url` |
| DELETE | `/api/v1/projects/{pid}` | 删除项目及其数据目录（需 `?confirm={pid}`） |
| GET | `/api/v1/projects/{pid}/stats` | `{counts, pass_rate, recent_runs:[摘要], last_run, trend:[{date,passed,failed,...}]}` |

- `pass_rate` 为 **0~1 的比例**（保留 4 位小数），前端自行格式化为百分比。
- `counts` 的键为 `endpoint / test_case / suite / environment / run / run_case_result / import_batch`。
- `trend` 按时间正序：`[{id, started_at, status, total, passed, failed, errored, incomplete, skipped, duration_ms}]`。

项目对象：

```json
{ "id": "hapi-air", "name": "hapi-air 接口测试", "description": "", "base_url": "http://127.0.0.1:8080",
  "created_at": "...", "updated_at": "..." }
```

## 3. 接口目录（endpoints）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/projects/{pid}/endpoints` | 查询参数 `folder, q, method, tag, limit, offset` |
| POST | `/api/v1/projects/{pid}/endpoints` | 新建（`spec.signature` 重复返回 409） |
| GET/PUT/DELETE | `/api/v1/projects/{pid}/endpoints/{id}` | 详情 / 更新 / 删除 |
| POST | `/api/v1/projects/{pid}/endpoints/{id}/to-case` | body `{name?, generate_assertions?}`，由接口生成用例 |
| GET | `/api/v1/projects/{pid}/folders` | `{endpoint_folders:[], case_folders:[], tags:[]}` |
| POST | `/api/v1/projects/{pid}/endpoints/{id}/probe` | 同步发起一次请求并返回 `HttpResponse`（不写历史） |

## 4. 用例（cases）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/projects/{pid}/cases` | 查询参数 `folder, q, tag, enabled, priority, limit, offset` |
| POST | `/api/v1/projects/{pid}/cases` | 新建；校验失败返回 400 且 `details` 为错误数组 |
| GET/PUT/DELETE | `/api/v1/projects/{pid}/cases/{id}` | 详情 / 更新 / 删除 |
| POST | `/api/v1/projects/{pid}/cases/{id}/duplicate` | 复制用例 |
| POST | `/api/v1/projects/{pid}/cases/{id}/run` | body `{env_id?, variables?, persist?, timeout_s?}`，同步执行单条用例；默认保存历史并返回 `{result: CaseResult, run: RunResult}`，`persist:false` 仅返回 `{result: CaseResult}` |

单条运行的 `variables` 在两种模式下均覆盖用例和环境中的同名变量，不修改用例或环境。运行历史中的请求、响应及参数快照使用现有脱敏规则；真实参数仅在执行内存中保留，参数签名以 SHA-256 摘要保存。`timeout_s` 是同步等待时间（默认 120 秒），长流程编辑页会按步骤等待上限和请求超时增加等待时间。

用例编辑页按执行顺序展示 `setup` 和最终请求，可展开查看请求、断言与提取器。运行参数表单从 `required_variables` 与变量引用生成，排除此前步骤已提取的变量和执行器内置时间变量。输入留空时继承用例或所选环境，环境中已保存的密钥仅提示已配置。`beneficiary_data` 校验 JSON 对象文本并保持字符串类型发送。

CaseSpec.setup 中的步骤另支持 `poll_timeout_ms`、`poll_interval_ms`、`poll_read_only`、`required_variables`，默认值保持旧用例行为。步骤的必填提取器失败会阻断执行；StepResult 增加 `attempts`。提取器 source 增加 `json_path_all`；认证 type 增加 `aliyun_sls`（username/password/token 为 AccessKey ID/Secret/可选 STS Token）。这些字段直接随既有用例及环境接口读写，详见 [模块契约](INTERFACES.md)。

运行未提供 env_id 时优先使用套件绑定的 env_id；failure_strategy=stop 会在条件不完整时停止剩余用例。

## 5. 套件（suites）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/projects/{pid}/suites` | 列表（含 `case_count`） |
| POST | `/api/v1/projects/{pid}/suites` | 新建 |
| GET/PUT/DELETE | `/api/v1/projects/{pid}/suites/{id}` | 详情 / 更新 / 删除 |

## 6. 环境（environments）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/projects/{pid}/environments` | 列表；变量与认证中的敏感值已掩码 |
| POST | `/api/v1/projects/{pid}/environments` | 新建 |
| GET/PUT/DELETE | `/api/v1/projects/{pid}/environments/{id}` | 详情 / 更新 / 删除 |
| POST | `/api/v1/projects/{pid}/environments/{id}/default` | 设为默认环境 |

> 掩码值 `******` 在写回时表示“保持原值不变”，不会把掩码写入数据库。

**“当前环境”的语义**：界面右上角的环境切换器调用的就是 `POST /environments/{id}/default`——
即“不指定 `env_id` 时用哪个环境”。因此不存在“界面专用环境”这类隐藏状态：
CLI 与 CI 不传 `--env` 时和界面上看到的环境完全一致；任何显式传入的 `env_id` 都优先于它。
切换环境会同时改变 `base_url` 与该环境的变量（例如 `${token}`），用例本身不需要改。

## 7. 运行（runs）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/projects/{pid}/runs` | 触发一次运行 |
| GET | `/api/v1/projects/{pid}/runs` | 查询参数 `status, suite_id, limit, offset`；返回不含用例明细的摘要 |
| GET | `/api/v1/projects/{pid}/runs/{run_id}` | 含 `case_results` 的完整结果 |
| POST | `/api/v1/projects/{pid}/runs/{run_id}/cancel` | 请求取消；排队中直接取消 |
| GET | `/api/v1/projects/{pid}/runs/{run_id}/report` | `?format=html\|xml\|json`，返回对应内容类型（xml 为 `application/xml`） |
| GET | `/api/v1/projects/{pid}/runs/{run_id}/diff` | `?base={run_id}`，与基线运行对比断言差异 |
| GET | `/api/v1/projects/{pid}/runs/compare` | `?before=&after=` 同上（等价入口） |

`diff` 响应结构（固定 schema，前端无需猜测）：

```json
{ "run_id": "run_new", "base_run_id": "run_old",
  "same_fingerprint": false,
  "summary": { "cases": 10, "changed": 2, "added": 1, "removed": 0,
               "newly_failed": 1, "newly_passed": 1, "status_changed": 2 },
  "changes": [
    { "kind": "status_changed", "case_id": 3, "case_name": "登录 - 冒烟",
      "before": { "status": "passed", "assertions": 3 },
      "after": { "status": "failed", "assertions": 3 },
      "assertions": [ { "describe": "json_path[$.code] eq 0", "before": "passed", "after": "failed" } ] }
  ] }
```

`kind` 取值：`added`（本次新增用例）、`removed`、`status_changed`、`assertion_changed`。

触发 body：

```json
{ "suite_id": 1, "case_ids": [3,4], "env_id": 2, "trigger": "manual",
  "note": "", "operator": "", "request_id": "ui-8f31", "background": true }
```

- `suite_id` 与 `case_ids` 至少提供一个；两者都给时以 `case_ids` 为准。
- `request_id` 幂等键：24 小时内同 `request_id` 且参数一致，直接返回既有运行；参数不同返回 409 `conflict`。
- `background` 默认 `true`（立即返回 `queued` 状态的 run，前端轮询）；`false` 时同步等待结束。

运行对象见 `hapitest/models.py::RunResult`；用例结果状态语义：

| 状态 | 含义 | CI 影响 |
|---|---|---|
| `passed` | 断言全部通过 | 通过 |
| `failed` | 至少一条断言失败 | 失败 |
| `error` | 请求未完成（连接失败/超时/被拦截） | 失败 |
| `incomplete` | 缺执行条件或未执行（无 base_url、无断言、前置失败） | 失败 |
| `skipped` | 显式跳过（用例停用） | 记为跳过 |

## 8. 导入（imports）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/projects/{pid}/imports` | 导入批次历史 |
| POST | `/api/v1/projects/{pid}/imports/preview` | 只解析不写库，返回 `ImportResult` |
| POST | `/api/v1/projects/{pid}/imports` | 解析并写库，返回 `{stats, batch_id}` |
| POST | `/api/v1/projects/{pid}/imports/fetch` | 从 URL 抓取 OpenAPI（如 `http://host:8080/v3/api-docs`），返回 `{preview, url, bytes}` |
| POST | `/api/v1/projects/{pid}/imports/mcp` | 通过 MCP（Apifox MCP Server）同步接口 |

`/imports/mcp` 的请求体：

```json
{ "apifox_project_id": "1234567", "doc_id": "", "spec_file": "",
  "refresh": true, "timeout_s": 180, "preview": false, "mode": "merge",
  "command": "npx -y apifox-mcp-server@latest", "token_env": "APIFOX_ACCESS_TOKEN",
  "api_base_url": "", "options": { "generate_cases": true } }
```

响应：`preview=true` 时为 `{preview, mcp, mode}`，否则为 `{stats, batch_id, detected, mcp, source}`。
`mcp` 字段记录服务端名称/版本、实际使用的工具名、是否刷新过缓存与取回字节数，
用于回答“这次同步拿到的是不是最新数据”。

> **令牌只从服务端读取**：平台按 `token_env`（默认 `APIFOX_ACCESS_TOKEN`）先查进程环境，
> 再查项目根目录的 `.env.local` / `.env`；**不接受请求体传入令牌**，因此令牌不会出现在
> 浏览器、运行记录或接口日志中。需要 Node.js 18+（`npx` 随 Node 安装）。

body 通用字段：

```json
{ "source": "auto", "text": "...", "path": "/abs/or/relative/file.json",
  "url": "http://127.0.0.1:8080/v3/api-docs", "mode": "merge",
  "options": { "base_url": "", "folder_prefix": "", "generate_cases": true,
               "case_suffix": " - 冒烟", "include_deprecated": false, "tags": [] } }
```

`source` 取 `auto|openapi|swagger2|postman|apifox|curl|spring`；`mode` 取 `merge`（按 `method + path` 更新已有接口，保留用例）或 `replace`（先清空接口目录再导入，用例的外键置空）。

> **安全说明**：`path` 是**服务端本地路径**，只有 CLI 与受信脚本会用到；浏览器拿不到服务器文件系统，
> 前端上传一律走 `text`。因此平台默认只监听 `127.0.0.1`：如果要对外暴露服务，必须先加访问控制，
> 否则 `path` 会成为任意文件读取入口。

## 9. 导出

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/projects/{pid}/export` | 全量导出（接口、用例、套件、环境，环境变量按 `reveal=1` 决定是否明文） |

## 10. 前端调用约定

- 统一封装：`async function api(path, options)`，非 2xx 或 `ok=false` 抛 `Error(error.message)`。
- 轮询：运行详情以 800ms 间隔轮询，直到 `status` 不在 `queued|running`。
- 所有展示的请求内容已由后端脱敏；前端不得尝试还原。
