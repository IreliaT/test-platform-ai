# Airwallex 巴基斯坦 PKR 自动化测试

## 已同步内容

项目 `hapi-air`：14 个接口、18 条用例、4 个套件和独立环境「Airwallex PKR 沙箱」。同步命令只写测试资产，不创建订单：

```sh
python3 -m hapitest.packs.airwallex_pk --project hapi-air
```

重复执行按接口 method/path、用例 source_ref、套件名称更新本包，保留其他资产及已有环境变量。会刷新本包用例中的步骤和断言；定制用例请先复制。数据在本地 `data/`，不进入 Git。

| 套件 | 内容 |
| --- | --- |
| 提现审核回调全流程 | 成功、失败各自创建新申请，按顺序执行 |
| 成功流程 | 提现 → 审核 → 提交 → 查 SLS → PROCESSING → SENT → PAID → 主单成功 |
| 失败退款流程 | 提现 → 审核 → 提交 → 查 SLS → PROCESSING → SENT → FAILED → 主单退款 → USD 余额恢复 |
| 接口与参数校验 | 余额、PKR 渠道、机构、汇率/免费手续费、历史、审核记录、沙箱认证、缺参及无效签名拒绝 |

单独的创建、审核、提交、渠道查单/状态模拟、SLS 查询用例默认停用；完整流程中会执行对应接口。完整流程不依赖用例之间共享变量。套件并发为 1，失败或条件不完整时停止。

## 第一次运行

在「用例管理」打开完整流程，页面展示 16 个步骤及最终余额检查，点击步骤可查看请求、断言与自动提取变量。在「运行此用例」选择 **Airwallex PKR 沙箱**，按测试账号与提现、渠道沙箱、SLS 日志三组填写参数，然后运行。环境已有值会自动继承；本次填写的参数不修改用例或环境，历史中的密钥会脱敏。订单 ID、transfer ID、余额与渠道 Token 由流程自动提取，无需手填。

需要反复用于套件运行的配置，可在项目的「环境」选择 **Airwallex PKR 沙箱** 并补齐以下变量。在「套件」中选择对应套件及该环境运行。命令行也可运行：

```sh
python3 -m hapitest run --help
python3 -m hapitest run --project hapi-air --suite <同步返回的套件ID> --env <同步返回的环境ID>
```

| 变量 | 来源 / 用法 |
| --- | --- |
| `user_token` | 测试用户 token，用户接口请求头 `token` |
| `test_uid` | **同一测试用户**的 UID；使用专用账号，运行期间不要从其他设备创建提现 |
| `withdraw_tier_id` | `/fun-wallet/withdrawInfo` 中允许提现的 `withdrawId` 档位；不是内部订单 ID |
| `beneficiary_data` | 收款资料 JSON **字符串**；已填公开沙箱示例 IBAN 与虚构姓名，按账号要求补充资料 |
| `admin_token` | 有审核及 PayoutAdmin 权限的测试管理员 token，请求头 `X-Access-Token` |
| `airwallex_client_id` / `airwallex_api_key` | 当前沙箱 API Key；每条流程运行时获取新 Token，不使用文档中的过期 Token |
| `sls_access_key_id` / `sls_access_key_secret` | 阿里云 RAM 凭据，需有目标 Logstore 的 `log:GetLogStoreLogs` 权限；网页登录态不能替代 API 签名 |
| `sls_security_token` | 使用 STS 时填写，否则留空 |
| `sls_base_url` / `sls_logstore` | 已填当前测试日志区域、Project 和 Logstore；不同环境须调整 |
| `airwallex_auth_base_url` / `airwallex_base_url` | 按回调文档分别使用 api-demo 登录、api.sandbox 查单与模拟；仅允许沙箱域名 |
| `channel_product_id` | 固定 `0-PK-airwallex-airwallex:bank_transfer-PKR` |

环境初始不含真实 Token、API Key 或 RAM Key。必填项缺失时第一步即返回 `incomplete`，不会发起提现。测试账号须有可提现余额、提现资格和额度；服务端须使用与沙箱账号一致的密钥，且沙箱 webhook 配置须能访问测试服务的 `/airwallex/callback`。

`beneficiary_data` 示例（在变量值中填写这一段完整 JSON 文本，平台会将其作为字符串序列化到 `data`）：

```json
{"accountName":"Test Recipient","iban":"PK36SCBL0000001123456702"}
```

可补充 `personalEmail`、`city`、`postcode`、`streetAddress` 等渠道要求的资料；使用测试资料。

## 查日志和回调怎么串联

1. 保存提现前 USD 余额及该用户上一笔内部订单 ID。
2. 校验币种 PKR、实际金额大于 0、手续费为 `0 PKR`，再申请提现。`JsonXView` 成功码是 **200**。
3. 按 UID、Airwallex、PK、PKR 查询审核记录，筛选 ID 大于上一笔的记录。须恰好有一条且为待审核，才提取 `withdraw_order_id`。
4. 审核只更新这个 ID，附带待审核状态与 UID 条件；提交只包含这一个 ID。两个写步骤均只执行一次。
5. SLS GetLogsV2 每 3 秒查一次，最多 120 秒：

   ```text
   Airwallex提现 and msg:"创建代付订单" and "awx-withdraw-${withdraw_order_id}"
   ```

   时间窗口从本条用例开始前 5 秒到当前时间。只在同一 `CreateTransferResult` 的 `id=<UUID>, requestId=awx-withdraw-<本订单ID>,` 中提取 transfer ID；其他订单日志不会被使用。SLS 查询结果须 `Complete`。

6. 登录获取 Token 后查渠道订单，核对 transfer ID、request_id、USD 来源、PKR 金额和 LOCAL 路由。
7. 按文档模拟 `PROCESSING → SENT → PAID/FAILED`。只有 PROCESSING 步骤允许失败后继续；SENT 和终态失败即停止。FAILED 默认 `failure_type=INVALID_ACCOUNT_NAME_OR_NUMBER`。
8. 模拟端点由 **Airwallex 发送带签名的 webhook**。平台不伪造签名直接跳过业务验签。SENT 后主单仍应为 `6`；PAID 后为 `2`；FAILED 经退款后为 `4`。
9. 轮询主单最多 120 秒。成功核对 USD 余额与扣款后快照一致；失败核对 USD 余额恢复到提现前快照。单独出现 REFUND 状态不能证明退款到账。

过程中停止的记录保留在运行详情里，可看到内部订单 ID、transfer ID、请求、响应、断言及尝试次数。暂停或失败后不要盲目重跑写步骤；先核对这笔已创建订单。完整流程每次运行会创建新的测试提现，受业务日额度限制。

## 执行器契约

- `setup[].poll_timeout_ms`：默认 0（只执行一次），上限 300000；`poll_interval_ms` 默认 1000。
- GET/HEAD 可轮询；POST 只读查询须显式 `poll_read_only=true`。提现、审核、提交、模拟状态不配置轮询。
- `setup[].required_variables` 在步骤发送前检查；首步骤列出完整流程需要的凭据。
- `extractors[].required=true` 未提取到值会使步骤失败，阻止后续请求。
- 提取表达式、断言路径及期望值支持 `${var}`；`json_path_all` 提取所有匹配成数组，用于确认新订单唯一。
- `_case_started_epoch` 和 `_now_epoch` 是执行器生成的秒级时间窗变量，后者每次请求刷新。
- SLS `auth.type=aliyun_sls`：username=AccessKey ID、password=Secret、token=可选 STS Token；每次请求动态签名，禁用重定向。
- Token 响应、提取值、相关断言和认证头在运行记录中脱敏。

## 验证边界与来源

`tests/test_workflow.py` 使用本机 HTTP 模拟服务验证两条真实执行器流程、SLS 日志迟到、错单排除、异步落账/退款、PROCESSING 已推进、必填配置缺失、超时和禁止重放写请求。模拟服务通过不代表真实业务环境通过；真实运行还需配置上述环境条件。

- 业务契约：`hapi-air-v2` 的 `FunWalletController`、`AdmCommonLogController`、`AdmPayoutController`、`AirwallexCallbackController`、`CalculateActualAmountResult`、`AirwallexPakistanPKRPayoutActualAmountHandler`、`PayoutHandlerAirwallexImpl`、`AirwallexNoticeServiceImpl`。
- [项目回调文档](https://alidocs.dingtalk.com/i/nodes/EpGBa2Lm8aM1OZ22UzavnjYM8gN7R35y)：2026-10-12 已通过用户打开的文档核对，未复制其中的凭据。
- [Airwallex 状态模拟文档](https://www.airwallex.com/docs/developer-tools/sandbox-environment/payouts/simulate-transfer-status-transition)：POST transition、next_status、版本及失败原因。
- [SLS GetLogsV2](https://www.alibabacloud.com/help/en/sls/developer-reference/api-sls-2020-12-30-getlogsv2)：POST 查询、秒级范围、gzip、Complete/Incomplete。
- [SLS 签名协议](https://www.alibabacloud.com/help/en/sls/developer-reference/request-signatures)、[官方 AuthV1 实现](https://github.com/aliyun/aliyun-log-python-sdk/blob/master/aliyun/log/auth.py)：0.6.0 HMAC-SHA1 / RAM / STS。
