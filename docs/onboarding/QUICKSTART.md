# 快速接入

目标：把业务接口同步进平台并跑出第一份回归结果。全程不需要联网安装依赖。

## 0. 前置

- Python 3.9+（`python3 -V` 能看到版本即可）
- 一个可以访问的被测环境（本地起服务、测试环境地址都行）

## 1. 启动平台

```sh
cd hapi-test-platform
python3 -m hapitest serve            # 打开 http://127.0.0.1:8801/
```

想先看效果，可以加 `--demo` 创建演示项目，并另开终端启动演示被测服务：

```sh
python3 -m hapitest serve --demo
python3 examples/mock_service.py     # http://127.0.0.1:8899，同时提供 /v2/api-docs
```

演示项目自带「演示套件」，直接运行即可看到 7/7 通过。也可以试试“从文档地址同步”：

```sh
python3 -m hapitest project create --name "演示（URL 导入）" --id demo-url
python3 -m hapitest import --project demo-url --url http://127.0.0.1:8899/v2/api-docs
python3 -m hapitest run --project demo-url --all
```

这条路径会从文档里探测 `host` 并自动写入默认环境，无需手填 `base_url`；
其中 3 条带路径参数的用例会失败——因为文档没给出 `id` 的示例值，导入时已经明确提示过，
按业务数据把路径参数改成真实存在的 ID 即可通过。这正是平台刻意保留的行为：**不猜业务数据**。

## 2. 建项目并配置环境

界面：`项目 → 新建`，填名称与 `base_url`；或命令行：

```sh
python3 -m hapitest project create --name "hapi-mamu 接口测试" --base-url http://127.0.0.1:8081
```

`base_url` 的优先级：用例 > 环境 > 项目。三处都没有时用例会判为 `incomplete`，不会被当成通过。

## 3. 同步接口（选一种）

### 3.0 Apifox（MCP，推荐给以 Apifox 为契约事实源的团队）

先在项目根目录的 `.env.local` 里放访问令牌（该文件已被 `.gitignore` 排除，不会进版本库）：

```sh
printf 'APIFOX_ACCESS_TOKEN=%s\n' '你的令牌' >> .env.local
```

令牌在 Apifox 里这样拿：右上角头像 →**账号设置 → API 访问令牌**→ 新建。
项目 ID 打开项目后在地址栏/项目设置里可以看到。

```sh
python3 -m hapitest import --project hapi-mamu --mcp-project 1234567 --preview   # 先看解析结果
python3 -m hapitest import --project hapi-mamu --mcp-project 1234567             # 再入库

# 接口在迭代分支上：必须指定分支，否则读到的是主分支（常见误判："项目里只有几条接口"）
python3 -m hapitest import --project hapi-mamu --mcp-project 1234567 --mcp-branch-id 12345

# 契约更新后，把改动同步到已生成的冒烟用例（默认只增不改，保护你手改过的用例）
python3 -m hapitest import --project hapi-mamu --mcp-project 1234567 --refresh-cases --preview
```

也可以直接用封装脚本：

```sh
./scripts/sync_apifox.sh hapi-mamu 1234567                          # 预览
./scripts/sync_apifox.sh hapi-mamu 1234567 --branch 12345 --import  # 指定分支并入库
./scripts/sync_apifox.sh hapi-mamu 1234567 --import --refresh-cases # 刷新冒烟用例
```

界面里同样可用：**导入 → Apifox（MCP）→ 填项目 ID → 从 Apifox 同步并预览 → 确认导入**。

前提：本机有 Node.js 18+（`npx` 随 Node 安装）。平台会临时拉起
`npx -y apifox-mcp-server@latest`，握手后调用它的文档读取工具；
"同步前强制刷新" 默认勾选，避免读到 Apifox MCP 的本地缓存。

> 令牌只从服务端环境变量或 `.env.local` 读取，页面不传、不显示、不落库。
> 私有化部署的 Apifox 用 `--mcp-api-base-url https://apifox.your-company.com`。

### 3.1 有 Swagger/OpenAPI 文档

本工作区的 Java 服务多为 springfox 2.7（Swagger 2.0），分组名要带上：

```sh
python3 -m hapitest import --project hapi-mamu \
  --url 'http://127.0.0.1:8081/v2/api-docs?group=api-3'
```

springdoc（OpenAPI 3）用 `/v3/api-docs` 或 `/v3/api-docs.yaml`。

> 注意：多数项目的 Docket 只在 dev/test 环境开启；生产环境文档地址通常是关闭的。
> 若返回 HTML 登录页，平台会明确报"看起来是 HTML 页面"，这时需要放行文档路径或补 `--header`。

### 3.2 只有源码（服务没起、或 Swagger 没暴露全部控制器）

```sh
python3 -m hapitest import --project hapi-mamu --scan ../hapi-mamu-v2 --preview   # 先看结果
python3 -m hapitest import --project hapi-mamu --scan ../hapi-mamu-v2             # 再入库
```

源码扫描会读取 `@RestController`/`@RequestMapping`/`@GetMapping` 等注解、路径常量、
`application*.yml` 里的 `server.port` / `context-path` / `spring.application.name`，
并把读取到的限制写入 warning（例如"类未声明类级 @RequestMapping"）。

### 3.3 其它来源

```sh
python3 -m hapitest import --project x --file collection.json          # Postman v2.1
python3 -m hapitest import --project x --file apifox.json              # Apifox 导出（OpenAPI 或原生）
python3 -m hapitest import --project x --curl 'curl -X POST http://h/api/x -H "X-Token: t" -d @body.json'
```

导入是幂等的：按 `method + path` 更新接口，已存在的用例不会重复创建，可以反复同步。

## 3.5 切换环境（右上角）

顶栏「环境」下拉列出当前项目的所有环境（显示 `名称 · base_url`），选一个即切换：
之后不指定环境的**试跑、运行、变量解析、导入时探测到的地址填充**都用它。

- 切换等价于把该环境设为项目的默认环境（`POST /environments/{id}/default`），
  所以 CLI `python3 -m hapitest run --project X --all` 不带 `--env` 时行为一致；
- CI 里请显式传 `--env <id>`，不要让流水线依赖"某次界面上的切换"；
- 下拉右侧的图标进入「环境」页，可增删环境、改 `base_url`、维护变量与认证。

## 4. 补用例

导入时会为每个接口生成一条冒烟用例，默认断言：

1. 响应体 `$.code == 0`（当响应 schema 含 `code` 字段时）
2. 状态码 `in [200, 201, 204]`
3. 响应时间 `< 3000ms`
4. `$.data` 存在
5. 响应 schema 中 `required` 字段存在（最多 5 个）

在界面「用例」页按业务语义调整：改断言期望值、加 `json_path` 断言、用「提取器」把
`$.data.token` 存成变量供后续用例使用、用「前置步骤」做登录串联。

> **路径参数**：如果契约没有给出 `example`/`default`/`enum`，导入器会按类型填占位值（如 `0`），
> 平台会在导入警告里逐条列出这些接口。请把它们改成真实存在的业务 ID，否则用例必然 404。

## 5. 组套件并运行

界面：`套件 → 新建`，勾选用例、选环境、设置失败策略；然后 `运行`。

命令行（CI 用）：

```sh
python3 -m hapitest run --project hapi-mamu --all \
  --junit reports/junit.xml --report-dir reports --json reports/run.json
echo $?      # 0 通过 / 1 断言失败 / 2 用法错误 / 3 缺条件或请求失败
```

`--all` 跑全部启用用例；也可以用 `--suite <id>`、`--case <id>`（可重复）、`--tag <标签>` 选择范围。

## 6. 看结果

- 界面「运行」页：计数器、逐用例明细（请求、响应、断言期望 vs 实际、提取变量、错误）。
- 报告：`?format=html`（人看）、`?format=xml`（Jenkins/GitLab）、`?format=json`（二次处理）。
- 「与基线对比」：回答"这次失败是不是新引入的"。`same_fingerprint=false` 说明用例定义已变化，
  差异只能作为关联证据。

## 7. 接进 CI

```yaml
# GitLab 示例
api-regression:
  script:
    - python3 -m hapitest run --project hapi-mamu --all --junit reports/junit.xml
  artifacts:
    when: always
    reports:
      junit: reports/junit.xml
```

## 常见问题

| 现象 | 原因与处理 |
|---|---|
| 全部用例 `incomplete`，错误含 `未配置 base_url` | 用例/环境/项目三处都没有地址，去「环境」页填 `base_url` |
| 全部用例 `failed`，实际状态码 404 | 目标地址不对（可能指向了别的服务），检查 `base_url` 与 `context-path` |
| 导入报"看起来是 HTML 页面" | 文档地址需要鉴权或路径不对；用浏览器确认 `?group=` 名称 |
| 提示 `执行队列已满` | 后台并发打满，稍后重试或调大 `HAPITEST_MAX_WORKERS` |
| 端口被占用 | `./start.sh status` 查看；或 `--port` 换端口，平台不会抢占端口 |
| 敏感值显示 `******` | 属于预期行为；环境变量回填 `******` 表示"保持原值" |
