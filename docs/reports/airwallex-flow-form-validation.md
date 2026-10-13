# 提现全流程步骤与参数表单验收

验证日期：2026-10-13。变更仓库：`hapi-test-platform`。

## 交付

- 用例编辑页优先展示 16 个流程步骤与最终余额检查；单步和全部展开可查看请求、断言、提取器与等待上限。
- 运行前提供 15 个参数输入项（14 个必填、1 个可选），分为测试账号与提现、Airwallex 沙箱、SLS 日志三组。
- 已提取的订单 ID、渠道 UUID、渠道 Token、余额快照及执行器时间变量不要求手填。环境已有值可继承，输入值仅覆盖本次执行，不改用例或环境。
- 单条运行保存历史时传递本次参数；运行快照脱敏、签名摘要保存，执行完成后释放内存参数。
- 收款资料校验 JSON 对象并保持字符串发送；输入缺失或格式错误时在页面上阻止提交。长流程延长同步等待时间。

## 验证

| 检查 | 状态 | 证据与范围 |
| --- | --- | --- |
| `python3 -m unittest discover -s tests -t .` | passed | 600 项；本地 mock 服务，无业务外网请求 |
| `node --test tests/test_workflow_ui.mjs` | passed | 6 项；两种完整流程的参数清单、提取顺序、可选变量、继承、JSON 字符串和 ID 类型 |
| `node --check` | passed | `hapitest/web/views/cases.js`、`hapitest/web/workflow.js` |
| `python3 -m hapitest doctor` | incomplete | 当前平台自身占用 8801；其余检查 ready |
| `HAPITEST_PORT=8802 python3 -m hapitest doctor` | passed | 临时使用可用端口，不更改平台持久配置 |
| `python3 -m hapitest verify --json /tmp/hapi-flow-selfcheck.json` | passed | 19/19；导入、执行四种终态、报告、项目隔离 |
| 页面刷新与交互 | passed | 实际打开成功流程，确认 17 张卡片与 15 个输入项；展开全部、切换运行环境、继承默认值和前端缺参/JSON 校验 |
| 控制台及资源 | passed | 无 error/warn 日志；页面资源均来自本机，没有增加 CDN 依赖 |
| 业务运行数核对 | passed | 缺参表单验证前后历史均为 18 条，没有发起真实提现 |
| `git diff --check` | passed | 无空白错误 |

截图：[参数表单](airwallex-flow-form.jpg)。

## 边界与收尾

本地平台已加载更新，成功流程页面可直接使用。真实业务流程未运行（`not_run`），实际执行仍需用户填写有效沙箱账号与配置。

保留仓库原有未提交改动；没有提交、推送或发布。`WIKI_UPDATE=not_applicable`：提现业务术语、订单关联与终态判定未改变，已有 Wiki 边界仍适用；界面与运行参数契约记录在本仓库的 HTTP_API、INTERFACES 与 AIRWALLEX_PK 文档。
