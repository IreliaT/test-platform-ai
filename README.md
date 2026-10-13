# HapiTest · 接口自动化测试平台

业务代码写完之后，把接口同步进来，就能在这个平台上做回归。

- **零依赖**：Python 3.9+ 标准库即可运行，`clone` 之后一条命令启动，不需要联网装包。
- **零构建**：前端是原生 HTML/CSS/ES Module，没有 npm、没有打包步骤。
- **环境随手切**：右上角选环境，试跑 / 运行 / 变量解析立刻跟着换（等价于改该项目的默认环境，CLI 与 CI 不传 `--env` 时行为一致）。
- **一平台多项目**：每个项目独立 SQLite 与数据目录，项目之间互不可见。
- **接口即测试**：从 OpenAPI / Swagger / Postman / Apifox / curl / Spring 源码同步接口，自动生成冒烟用例。
- **结果诚实**：`passed / failed / error / incomplete`，缺条件、没断言一律不算通过。

## 30 秒上手

```sh
cd hapi-test-platform
cp .env.example .env.local            # 可选，默认配置即可跑
python3 -m hapitest serve --demo       # 启动并创建演示项目
# 打开 http://127.0.0.1:8801/
```

另开一个终端启动演示用的被测服务，然后在界面上运行「演示项目」的用例：

```sh
python3 examples/mock_service.py       # http://127.0.0.1:8899，同时提供 /v2/api-docs
```

想直接体验“从文档地址同步接口”，可以走一遍 URL 导入（会自动探测文档里的 host 写入环境）：

```sh
python3 -m hapitest project create --name "演示（URL 导入）" --id demo-url
python3 -m hapitest import --project demo-url --url http://127.0.0.1:8899/v2/api-docs
python3 -m hapitest run --project demo-url --all
```

或者后台常驻：

```sh
./start.sh start     # 启动
./start.sh status    # 状态 + 健康检查
./start.sh stop      # 停止
```

## 业务流程

```
业务代码完成
    │  ① 同步接口：OpenAPI/Swagger 文档、Postman 集合、Apifox 导出、curl 命令、Spring 源码扫描
    ▼
平台接口目录（endpoint）
    │  ② 自动生成冒烟用例（默认断言：状态码 + 响应时间 + $.code == 0 + $.data 存在）
    ▼
用例（case）—— 手工补断言、提取变量、加前置步骤（登录串联）
    │  ③ 组成套件（suite），选择环境（environment）
    ▼
运行（run）—— 后台队列执行，结果与历史入库
    │  ④ 报告：Web 页面 / JUnit XML / HTML / JSON；与基线运行对比差异
    ▼
CI 门禁（`python3 -m hapitest run --junit …`，非 0 退出码）
```

## 同步接口的几种方式

| 方式 | 命令 | 适用场景 |
|---|---|---|
| Apifox（MCP） | `--mcp-project <项目ID>` | **Apifox 是契约事实源**：直接通过 Apifox MCP Server 拉取，无需手工导出 |
| OpenAPI / Swagger 文档 | `--file openapi.json` / `--url http://host:8080/v3/api-docs` | 已有 springdoc / springfox / knife4j 的服务 |
| springfox（本工作区现状） | `--url 'http://host:8081/v2/api-docs?group=api-3'` | hapi 系列 Java 服务（Swagger 2.0） |
| Postman 集合 | `--file collection.json` | 团队已有 Postman 资产 |
| Apifox 导出文件 | `--file apifox.json` | 离线导出（OpenAPI 或 Apifox 原生 JSON） |
| curl 命令 | `--curl 'curl -X POST …'` | 单个接口快速落库 |
| Spring 源码扫描 | `--scan /path/to/repo` | 服务没开、或 Swagger 未暴露全部控制器 |

```sh
# Apifox（MCP）：先把访问令牌写进 .env.local（该文件已被 .gitignore 排除）
#   APIFOX_ACCESS_TOKEN=xxxxx
python3 -m hapitest import --project hapi-mamu --mcp-project 1234567
python3 -m hapitest import --project hapi-mamu --mcp-project 1234567 --preview    # 只预览
python3 -m hapitest import --project hapi-mamu --mcp-project 1234567 \
  --mcp-branch-id 12345 --refresh-cases                                          # 指定分支 + 刷新冒烟用例

python3 -m hapitest import --project hapi-mamu --scan ../hapi-mamu-v2 --preview
python3 -m hapitest import --project hapi-mamu --url 'http://127.0.0.1:8081/v2/api-docs?group=api-3'
```

同一次契约可以反复同步：按 `method + path` 更新接口，已有用例不会被重复创建。

三条与"真实项目会踩"相关的约定：

- **分支**：Apifox MCP 默认读**主分支**；接口在迭代分支上时必须加 `--mcp-branch-id`，否则会误以为"项目里只有几条接口"。
- **用例刷新**：默认只增不改（不会悄悄覆盖你调过的用例）；契约变了想同步到已生成的冒烟用例，加 `--refresh-cases`，它只覆盖请求模板与断言，保留名称/目录/优先级/启停/标签。
- **参数策略**：必填参数一定带上；可选参数**只要契约里有示例值也带上**（否则真实服务常返回 400/500）；敏感参数不复制凭据，改写成 `${参数名}`，在环境变量里定义同名变量即可跑通。
- 契约没有给出示例值的路径参数会在导入警告里逐条列出——平台不猜业务数据。

## CI 用法

```sh
python3 -m hapitest run --project hapi-mamu --all \
  --junit reports/junit.xml --report-dir reports --json reports/run.json
```

`--all` 跑全部启用用例，也可用 `--suite <id>`、`--case <id>`（可重复）、`--tag <标签>` 限定范围。

退出码：`0` 全部通过，`1` 断言失败，`2` 用法/配置错误，`3` 缺少执行条件或请求未完成。
Jenkins 用 `junit 'reports/junit.xml'`，GitLab 用 `artifacts:reports:junit`。

## 目录结构

```
hapi-test-platform/
├── hapitest/                 # 平台源码（Python 包）
│   ├── models.py             # 规范化数据模型（字段契约）
│   ├── db.py repository.py   # SQLite 存储与项目级仓储
│   ├── http_client.py        # 标准库 HTTP 客户端（超时/重定向/压缩/脱敏）
│   ├── assertions.py         # 断言引擎（27 种操作符）
│   ├── jsonpath.py           # JSONPath 子集
│   ├── variables.py          # ${var} / {{var}} 变量与提取器
│   ├── runner.py             # 执行引擎：队列、并发、取消、重试
│   ├── report.py             # JUnit XML / HTML / JSON 报告
│   ├── importers/            # OpenAPI、Postman、Apifox、curl、Spring 扫描、YAML 子集解析
│   ├── server/               # HTTP API + 静态资源
│   └── web/                  # 前端（无构建步骤）
├── examples/                 # mock 服务 + 演示契约
├── tests/                    # unittest 测试
├── docs/                     # 架构、接口契约、接入说明、验收报告
├── scripts/                  # 验证脚本
└── data/                     # 运行数据（不提交）
```

## 常用命令

```sh
python3 -m hapitest doctor                       # 体检：配置/目录/项目/端口
python3 -m hapitest verify --json out.json       # 端到端自检（本地 mock，不联网）
python3 -m hapitest project list                 # 项目列表
python3 -m hapitest project create --name 我的项目 --base-url http://127.0.0.1:8080
python3 -m hapitest run --project my --suite 1   # 命令行执行
```

## 关键约定

- **脱敏**：`password / token / secret / authorization / cookie` 等字段名命中的值在落库、返回界面、写报告时统一替换为 `******`；环境变量里回传 `******` 表示"保持原值"。
- **不说谎的通过**：没有 `base_url`、没有断言、前置步骤失败 → `incomplete`；请求超时/连不上 → `error`。只有断言全部通过才是 `passed`。
- **幂等**：`POST /runs` 带 `request_id` 时，24 小时内同参数重复提交返回同一个运行。
- **出网限制**：默认禁止云元数据地址；可用 `HAPITEST_ALLOW_HOSTS` 限制只允许访问测试域名。
- **不存凭据**：平台不写业务密码；`.env.local` 与 `data/` 均不进入版本库。

## 与 TestDock 的关系

架构思路参考了 [iNorthward/TestDock](https://github.com/iNorthward/TestDock) 的 Core/Pack 边界、四态结果语义、
持久任务与来源指纹，但按"单团队内部平台"的规模做了取舍：

- 保留：项目隔离、四态结果与"不完整不算通过"、请求幂等、运行指纹与历史对比、离线自检。
- 简化：不做多 Pack 进程隔离、不做 API 版本协商、不做多主题体系、不做发行指纹。
- 加强：面向本工作区的 Spring/Swagger 2.0 同步路径，以及源码扫描兜底。

## 文档

- [快速接入](docs/onboarding/QUICKSTART.md)
- [架构说明](docs/architecture/ARCHITECTURE.md)
- [模块接口契约](docs/architecture/INTERFACES.md)
- [HTTP API](docs/architecture/HTTP_API.md)
- [Airwallex 巴基斯坦 PKR：接口同步、成功/失败退款流程及环境配置](docs/testing/AIRWALLEX_PK.md)
