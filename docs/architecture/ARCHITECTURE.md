# HapiTest 架构说明

## 1. 定位

HapiTest 是一个**与被测系统解耦**的接口自动化测试平台。它不感知业务实现，只做四件事：

1. 把接口契约（OpenAPI / Swagger / Postman / Apifox / curl / Spring 源码）规范化成平台模型；
2. 把接口组织成可执行的用例与套件；
3. 按环境发真实 HTTP 请求并判断断言；
4. 保存运行证据（请求、响应、断言、变量、耗时）并输出报告。

## 2. 分层

```
┌──────────────────────────────────────────────────────────────┐
│ 接入层  importers/        openapi · postman · apifox · curl · spring_scan · yaml_lite
│         mcp_client.py     通过 MCP（Apifox MCP Server）直接拉取接口文档
├──────────────────────────────────────────────────────────────┤
│ 契约层  models.py          EndpointSpec / CaseSpec / SuiteSpec / EnvironmentSpec / RunResult
│         INTERFACES.md     字段冻结文档，所有模块共用同一套字段名
├──────────────────────────────────────────────────────────────┤
│ 执行层  http_client.py     请求发送（超时、重定向、压缩、TLS、脱敏）
│         variables.py       变量解析与提取（${var} / {{var}} / ${var:-默认}）
│         assertions.py      27 种操作符 + JSON Schema 子集校验
│         jsonpath.py        JSONPath 子集
│         runner.py          队列、并发、重试、取消、终态判定
├──────────────────────────────────────────────────────────────┤
│ 存储层  db.py              项目级 SQLite（每项目一个文件）
│         repository.py      注册表 + 项目仓储（所有查询按项目边界）
│         history.py         运行对比
├──────────────────────────────────────────────────────────────┤
│ 交付层  server/            HTTP API（{ok,data,error} 信封）+ 静态资源
│         web/               前端控制台（hash 路由，无构建）
│         report.py          JUnit XML / HTML / JSON
│         cli.py             serve / import / run / doctor / verify
│         sync.py            导入落库（幂等）、远端规格抓取
└──────────────────────────────────────────────────────────────┘
```

依赖方向自上而下，禁止反向依赖；`models.py` 不 import 任何业务模块，`importers/` 不 import 仓储。

## 3. 项目隔离

- 平台级：`data/registry.json` 保存项目清单（id、名称、base_url）。
- 项目级：`data/projects/<id>/platform.db` 保存该项目全部数据；`data/projects/<id>/runs/<run_id>.json` 保存运行参数快照。
- 项目 ID 只允许 `a-z0-9_-`，非法 ID 与路径穿越在 `config.safe_project_id` 处被拒绝。
- 运行参数落盘使历史运行可以脱离内存解释；进程重启后仍能定位"这次跑了哪些用例、用了什么变量"。

## 4. 执行模型

```
POST /runs ──► RunManager.submit ──► 写 run(status=queued) ──► 队列
                                        │
                    worker 线程（max_workers，默认 4）取出
                                        │
                        按项目加锁（同项目串行，跨项目并行）
                                        │
              ┌─────────────────────────┴──────────────────────────┐
              │ CaseRunner.run_case                                │
              │  1. 停用 → skipped                                 │
              │  2. 无断言 / 无 base_url → incomplete              │
              │  3. 前置步骤（可提取变量，失败 → incomplete）      │
              │  4. 发送请求：失败 → error（按 retries 重试）      │
              │  5. 断言：全部通过 → passed，否则 failed           │
              │  6. 提取变量写回运行变量（敏感值落库前掩码）        │
              └────────────────────────────────────────────────────┘
                                        │
              run.recount() → finalize_status()（失败优先）→ 落库 + 报告
```

- **队列容量**：默认 32，满则返回 429 `queue_full`，运行标记为 `error` 而不是静默丢弃。
- **取消**：`cancel` 置标志位并立即把状态改为 `canceled`；执行线程在每个用例、每次重试前检查标志。
- **失败即停**：套件 `failure_strategy=stop` 时遇 `failed/error` 停止后续用例。
- **并发套件**：`concurrency>1` 时用例级并行，每个用例使用独立变量解析器，运行级变量不跨用例共享（避免隐藏依赖）。

## 5. 终态语义（核心约定）

| 状态 | 触发条件 | 是否算"通过" |
|---|---|---|
| `passed` | 断言全部通过 | 是 |
| `failed` | 至少一条断言失败 | 否 |
| `error` | 请求未完成（连接失败、超时、被安全策略拦截） | 否 |
| `incomplete` | 缺执行条件（无 base_url）、无断言、前置步骤失败 | 否 |
| `skipped` | 用例停用 | 否（单独计数） |

`RunResult.finalize_status()` 按 `failed > error > incomplete/skipped > passed` 优先级推导整体状态，
`exit_code` 只有全通过才是 0。

## 6. 安全与脱敏

- 出网限制：`check_url_allowed` 拒绝非 http(s)、拒绝 `HAPITEST_DENY_HOSTS`（默认含云元数据地址与 `169.254.*`）；配置 `HAPITEST_ALLOW_HOSTS` 后只允许命中后缀的主机。
- 脱敏：`redact.py` 按字段名关键字（password/token/secret/authorization/cookie/sign…）统一掩码，作用于请求预览、响应头、返回给界面的环境变量、报告与运行日志。
- 变量本身在执行期保留真实值（否则无法发请求），但**写入历史的提取变量会被掩码**。
- 平台不保存业务凭据到版本库；`.env.local`、`data/` 均在 `.gitignore` 中。

## 7. 数据一致性

- 每个项目一个 SQLite 文件，`check_same_thread=False` + 仓储级 `RLock` 串行化写操作。
- `ensure_schema()` 幂等，可对既有库重复执行；`schema_meta.schema_version` 记录版本，预留迁移入口。
- 导入幂等：`merge` 模式按 `method + path` upsert 接口；用例按 `source + source_ref + name` 判重。
- 运行保留：`vacuum_runs` 按 `HAPITEST_RUN_RETENTION`（默认 300）清理最旧的运行。

## 8. 与 CI 的接口

- `POST /runs` 支持 `request_id` 幂等键，重复触发不会产生重复执行。
- `run.fingerprint` 是用例定义集合的哈希；历史对比时 `same_fingerprint=false` 提示"定义已变化"，差异只能作为关联证据。
- `GET /runs/{id}/report?format=xml` 输出 Jenkins/GitLab 可解析的 JUnit XML（`timestamp` 不带时区后缀）。

## 9. MCP 接入通道

```
CLI/界面 ──► sync.load_import_from_mcp ──► mcp_client.fetch_apifox_openapi
                                              │ 启动 npx apifox-mcp-server（stdio）
                                              │ initialize → tools/list → tools/call
                                              └ 取回 OpenAPI 文本（必要时内联外部 $ref）
                                                        │
                                              parse_text(..., source=apifox) → 入库
```

- 工具名带随机后缀（如 `read_project_oas_sppywa`），因此**按前缀匹配**，不写死全名；
- 令牌只经子进程环境变量传递，且所有对外错误信息都做掩码（含服务端回显令牌的情况）；
- stdout 由独立线程读入队列，超时判断才真正生效（直接 `readline()` 会永久阻塞）；
- 每次调用都会关闭子进程与三条管道，避免长跑服务累积文件描述符。

## 10. 已知边界

- 不做进程级沙箱：用例在平台进程内的线程中执行，靠请求超时与取消标志控制；执行不受信任代码需要额外的进程隔离。
- 不做数据库校验：平台只做 HTTP 层验证，直连数据库比对不在当前范围。
- 前端不提供断言脚本编辑（不支持 Postman 的 `pm.*` JS 脚本），导入时会把可识别的 `pm.response.to.have.status()` / `pm.expect()` 翻译成平台断言，其余记录为警告。
