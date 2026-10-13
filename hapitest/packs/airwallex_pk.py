"""Airwallex 巴基斯坦个人 PKR 提现契约快照与完整沙箱流程。

来源：hapi-air-v2 控制器/DTO，以及 docs/testing/AIRWALLEX_PK.md 中列出的文档。
不包含凭据；install 只保存测试资产，绝不执行提现。
"""
from copy import deepcopy

from ..models import CaseSpec, EndpointSpec, EnvironmentSpec, SuiteSpec

PREFIX = 'airwallex-pk/v1/'
FOLDER = '提现 / Airwallex 巴基斯坦 PKR'
LOG_CLASS = 'com.mamu.funnyair.model.log.CurrencyWithdrawLog'
PRODUCT = '0-PK-airwallex-airwallex:bank_transfer-PKR'
DEFAULTS = {
    'user_token': '', 'admin_token': '', 'test_uid': '', 'withdraw_tier_id': '',
    'beneficiary_data': '{"accountName":"Test Recipient","iban":"PK36SCBL0000001123456702"}',
    'channel_product_id': PRODUCT,
    'airwallex_client_id': '', 'airwallex_api_key': '',
    'airwallex_base_url': 'https://api.sandbox.airwallex.com',
    'airwallex_auth_base_url': 'https://api-demo.airwallex.com',
    'sls_base_url': 'https://test-env-sg.ap-southeast-1.log.aliyuncs.com',
    'sls_logstore': 'hapi-common-log', 'sls_access_key_id': '', 'sls_access_key_secret': '',
    'sls_security_token': '',
}


def check(path, expected=None, operator='eq'):
    return {'type': 'json_path', 'path': path, 'operator': operator, 'expected': expected}


def variable(name, expected=None, operator='eq'):
    return {'type': 'variable', 'name': name, 'operator': operator, 'expected': expected}


def status(expected=200):
    return {'type': 'status_code', 'expected': expected}


OK = [status(), check('$.code', 200)]


def extract(name, expression, source='json_path', required=True, **extra):
    return dict(name=name, expression=expression, source=source, required=required, **extra)


def request(method, path, body=None, query=None, auth='user', base_url=None):
    result = {'method': method, 'path': path, 'auth': {'type': 'none'}, 'follow_redirects': False}
    if auth in ('user', 'admin'):
        result['headers'] = [{'name': 'token' if auth == 'user' else 'X-Access-Token',
                              'value': '${%s_token}' % auth}]
    elif auth == 'airwallex':
        result['auth'] = {'type': 'bearer', 'token': '${airwallex_token}'}
        result['headers'] = [{'name': 'x-api-version', 'value': '2025-11-11'}]
    elif auth == 'sls':
        result['auth'] = {'type': 'aliyun_sls', 'username': '${sls_access_key_id}',
                          'password': '${sls_access_key_secret}', 'token': '${sls_security_token:-}'}
        result['headers'] = [{'name': 'Accept-Encoding', 'value': 'gzip'}]
    if body is not None:
        result.update(body_type='json', body=body)
    if base_url:
        result['base_url'] = base_url
    if query:
        result['query'] = [{'name': key, 'value': value} for key, value in query.items()]
    return result


def log_list(fields=None):
    return request('POST', '/admin/log/common/list', fields or {
        'uid': '${test_uid}', 'channelId': 'airwallex', 'countryCode': 'PK', 'currency': 'PKR'},
        {'page': 1, 'limit': 100, 'logClass': LOG_CLASS}, 'admin')


def auth_request():
    result = request('POST', '/api/v1/authentication/login', auth='none', base_url='${airwallex_auth_base_url}')
    result['headers'] = [{'name': 'x-client-id', 'value': '${airwallex_client_id}'},
                         {'name': 'x-api-key', 'value': '${airwallex_api_key}'}]
    return result


def transfer_request():
    return request('GET', '/api/v1/transfers/{transfer_id}', auth='airwallex', base_url='${airwallex_base_url}')


def transition(next_status):
    body = {'next_status': next_status}
    if next_status == 'FAILED':
        body['failure_type'] = 'INVALID_ACCOUNT_NAME_OR_NUMBER'
    return request('POST', '/api/v1/simulation/transfers/{transfer_id}/transition', body,
                   auth='airwallex', base_url='${airwallex_base_url}')


def sls_query():
    return request('POST', '/logstores/{sls_logstore}/logs', {
        'from': '${_case_started_epoch}', 'to': '${_now_epoch}', 'line': 100, 'offset': 0, 'reverse': True,
        'query': 'Airwallex提现 and msg:"创建代付订单" and "awx-withdraw-${withdraw_order_id}"',
    }, auth='sls', base_url='${sls_base_url}')


def step(name, req, assertions=None, extractors=None, poll=False, **extra):
    value = dict(name=name, request=req, assertions=deepcopy(assertions if assertions is not None else OK),
                 extractors=extractors or [], **extra)
    if poll:
        value.update(poll_timeout_ms=120000, poll_interval_ms=3000, poll_read_only=True)
    return value


def definitions():
    withdraw = request('POST', '/fun-wallet/withdraw', {
        'withdrawId': '${withdraw_tier_id}', 'channelProductId': '${channel_product_id}',
        # data 是 JSON 字符串，不能改成对象。整串引用保留字符串类型并由 HTTP 层转义。
        'data': '${beneficiary_data}',
    })
    calculate = request('POST', '/fun-wallet/calculateActualAmount', {
        'withdrawId': '${withdraw_tier_id}', 'channelProductId': '${channel_product_id}'})
    approve = request('POST', '/admin/log/common/update-fields', {
        'id': '${withdraw_order_id}', 'logClass': LOG_CLASS,
        'fields': {'state': 1, 'remark': 'HapiTest Airwallex PKR'},
        'filters': {'state': 0, 'uid': '${test_uid}'},
    }, auth='admin')
    submit = request('POST', '/admin/payout/bathWithdraw', {
        'withdrawOrderIds': ['${withdraw_order_id}'], 'remark': 'HapiTest Airwallex PKR'}, auth='admin')
    callback = request('POST', '/airwallex/callback', {}, auth='none')
    callback['headers'] = [{'name': 'x-timestamp', 'value': '0'}, {'name': 'x-signature', 'value': 'invalid'}]
    templates = [
        ('wallet', '提现余额及档位', request('GET', '/fun-wallet/withdrawInfo')),
        ('channel', '巴基斯坦提现渠道', request('GET', '/fun-wallet/getChannelInfo', query={'countryCode': 'PK'})),
        ('org', '收款机构信息', request('GET', '/fun-wallet/getTargetOrg', query={'channelProductId': '${channel_product_id}'})),
        ('calculate', 'USD 换算 PKR 及零手续费', calculate),
        ('withdraw', '创建提现申请', withdraw),
        ('history', '钱包流水', request('GET', '/fun-wallet/history', query={'page': 1, 'limit': 10})),
        ('orders', '查询提现审核记录', log_list()),
        ('approve', '审核通过', approve), ('submit', '提交 Airwallex 渠道', submit),
        ('callback', '业务回调入口（验签拒绝）', callback),
        ('auth', '沙箱获取 Airwallex Token', auth_request()),
        ('transfer', '查询渠道订单状态', transfer_request()),
        ('transition', '沙箱模拟回调状态', transition('FAILED')),
        ('sls', 'SLS 查询渠道创建日志', sls_query()),
    ]
    endpoints = [EndpointSpec.from_dict(dict(name=name, folder=FOLDER, method=req['method'], path=req['path'],
                    request=req, tags=['airwallex', 'PKR'], source='template', source_ref=PREFIX + key,
                    description='业务接口以 hapi-air-v2 代码为准；完整流程及前提见 AIRWALLEX_PK.md。'))
                 for key, name, req in templates]
    fee_checks = OK + [check('$.data.currency', 'PKR'), check('$.data.handlingFees', '0'),
                       check('$.data.handlingFeesCurrency', 'PKR'), check('$.data.money', 0, 'gt')]
    cases = []
    for key, name, req in templates:
        enabled = key in ('wallet', 'channel', 'org', 'calculate', 'history', 'callback', 'orders', 'auth')
        required = ['user_token']
        if key == 'calculate':
            required += ['withdraw_tier_id']
        if key in ('orders', 'approve', 'submit'):
            required += ['admin_token', 'test_uid']
        checks = deepcopy(fee_checks if key == 'calculate' else [status(400)] if key == 'callback' else OK)
        if key == 'channel':
            checks.append({'type': 'body_text', 'operator': 'contains', 'expected': PRODUCT})
        guard = step('检查执行条件', request('GET', '/fun-wallet/withdrawInfo'), required_variables=required)
        if key == 'auth':
            guard = step('检查沙箱认证条件', auth_request(), [status(201), check('$.token', operator='not_empty')],
                         required_variables=['airwallex_client_id', 'airwallex_api_key'])
            checks = [status(201), check('$.token', operator='not_empty')]
        cases.append(CaseSpec.from_dict(dict(name=name + ' - 接口用例', folder=FOLDER, request=deepcopy(req),
            assertions=checks, setup=[] if key == 'callback' else [guard], enabled=enabled, source='template',
            source_ref=PREFIX + 'case/' + key, tags=['airwallex', '接口'], priority='P1',
            description='写操作及依赖渠道 ID 的单接口用例默认停用；请优先运行完整流程套件。')))
    for key, missing in [('missing-tier', 'withdrawId'), ('missing-data', 'data')]:
        invalid = request('POST', '/fun-wallet/withdraw', {'withdrawId': 'invalid', 'channelProductId': PRODUCT, 'data': '{}'})
        invalid['body'].pop(missing)
        cases.append(CaseSpec.from_dict(dict(name='提现缺少 ' + missing + ' 被拒绝', folder=FOLDER, request=invalid,
            assertions=[status(), check('$.code', 400)], setup=[step('检查用户认证', request('GET', '/fun-wallet/withdrawInfo'),
            required_variables=['user_token'])], source='template', source_ref=PREFIX + 'case/' + key, tags=['airwallex', '校验'])))
    required = ['user_token', 'admin_token', 'test_uid', 'withdraw_tier_id', 'beneficiary_data',
                'airwallex_client_id', 'airwallex_api_key', 'sls_access_key_id', 'sls_access_key_secret',
                'airwallex_base_url', 'airwallex_auth_base_url', 'sls_base_url', 'sls_logstore', 'channel_product_id']
    # 独立账号上以单调递增订单 ID 排除历史订单；要求恰好命中一条新申请。
    new_orders = '$.data.items[?(@.id > ${previous_order_id})]'
    for branch, terminal, main_state, balance in [('success', 'PAID', 2, 'wallet_after'), ('failure', 'FAILED', 4, 'wallet_before')]:
        setup = [
            step('提现前 USD 余额', request('GET', '/fun-wallet/withdrawInfo'),
                 OK + [check('$.data.currency', 'USD'), variable('channel_product_id', PRODUCT),
                       variable('airwallex_base_url', r'^(https://api(\.sandbox|-demo)\.airwallex\.com/?|http://127\.0\.0\.1:\d+)$', 'regex'),
                       variable('airwallex_auth_base_url', r'^(https://api(\.sandbox|-demo)\.airwallex\.com/?|http://127\.0\.0\.1:\d+)$', 'regex')],
                 [extract('wallet_before', '$.data.money')], required_variables=required),
            step('记录本用户上一笔订单 ID', log_list(), extractors=[extract('previous_order_id', '$.data.items[0].id', required=False, default=0)]),
            step('校验 PKR 金额和免费手续费', calculate, fee_checks, [extract('pkr_amount', '$.data.money')]),
            step('申请提现（只执行一次）', withdraw),
            step('查询本次新提现申请', log_list(), OK + [variable('new_orders', 1, 'length_eq'),
                 variable('withdraw_order_id', '${previous_order_id}', 'gt'), variable('withdraw_order_state', 0)],
                 [extract('new_orders', new_orders, 'json_path_all'), extract('withdraw_order_id', new_orders + '.id'),
                  extract('withdraw_order_state', new_orders + '.state')], poll=True),
            step('提现后 USD 余额已扣款', request('GET', '/fun-wallet/withdrawInfo'),
                 OK + [check('$.data.money', '${wallet_before}', 'lt')], [extract('wallet_after', '$.data.money')]),
            step('审核通过（仅本订单、仅待审核状态）', approve),
            step('提交本订单到渠道（只执行一次）', submit),
            step('等待 SLS 创建日志，提取本订单 transfer ID', sls_query(),
                 [status(), check('$.meta.progress', 'Complete')],
                 [extract('transfer_id', r'id=([0-9a-fA-F-]{36}),\s*requestId=awx-withdraw-${withdraw_order_id}(?=,)', 'regex')], poll=True),
            step('获取沙箱 Token', auth_request(), [status(201), check('$.token', operator='not_empty')],
                 [extract('airwallex_token', '$.token')]),
            step('核对渠道 ID / USD 来源 / PKR LOCAL 下单', transfer_request(), [status(),
                 check('$.id', '${transfer_id}'), check('$.request_id', 'awx-withdraw-${withdraw_order_id}'),
                 check('$.source_currency', 'USD'), check('$.transfer_currency', 'PKR'),
                 check('$.transfer_amount', '${pkr_amount}', 'approx'), check('$.transfer_method', 'LOCAL')]),
            step('模拟 PROCESSING（文档允许已推进时报错后继续）', transition('PROCESSING'),
                 [status(), check('$.status', 'PROCESSING')], continue_on_failure=True),
            step('模拟 SENT（仍为处理中）', transition('SENT'), [status(), check('$.status', 'SENT')]),
            step('SENT 不得将主单结算为成功', log_list({'id': '${withdraw_order_id}', 'uid': '${test_uid}'}),
                 OK + [check('$.data.items[0].state', 6)], poll=True),
            step('模拟 ' + terminal + ' 并由渠道发送真实签名回调', transition(terminal), [status(), check('$.status', terminal)]),
            step('等待业务回调落账：' + ('成功' if branch == 'success' else '失败并退回原 USD'),
                 log_list({'id': '${withdraw_order_id}', 'uid': '${test_uid}'}),
                 OK + [check('$.data.items', 1, 'length_eq'), check('$.data.items[0].id', '${withdraw_order_id}'),
                       check('$.data.items[0].state', main_state), check('$.data.items[0].currency', 'PKR'),
                       check('$.data.items[0].handlingFees', 0, 'approx')], poll=True),
        ]
        cases.append(CaseSpec.from_dict(dict(name='巴基斯坦 PKR 提现完整流程 - ' + ('回调成功' if branch == 'success' else '回调失败并退款'),
            folder=FOLDER + ' / 完整流程', description='使用专用沙箱测试用户；提交和提现不重试。失败检查状态 REFUND 和 USD 余额恢复。',
            request=request('GET', '/fun-wallet/withdrawInfo'), assertions=OK + [check('$.data.currency', 'USD'),
            check('$.data.money', '${%s}' % balance)], setup=setup, source='template', source_ref=PREFIX + 'flow/' + branch,
            tags=['airwallex', '流程', branch], priority='P0')))
    return endpoints, cases


def install(repository):
    """幂等同步专属资产；保留其他资产和已有环境变量，不执行业务请求。"""
    endpoints, cases = definitions()
    ids = {}
    for endpoint in endpoints:
        current = repository.find_endpoint_by_signature(endpoint.method, endpoint.path)
        saved = repository.update_endpoint(current.id, endpoint) if current else repository.create_endpoint(endpoint)
        ids[endpoint.source_ref.removeprefix(PREFIX)] = saved.id
    existing_cases = {case.source_ref: case for case in repository.list_cases(limit=10000)[0]}
    saved_cases = {}
    for case in cases:
        key = case.source_ref.removeprefix(PREFIX + 'case/')
        case.endpoint_id = ids.get(key)
        current = existing_cases.get(case.source_ref)
        saved = repository.update_case(current.id, case) if current else repository.create_case(case)
        saved_cases[case.source_ref] = saved.id
    env = next((item for item in repository.list_environments() if item.name == 'Airwallex PKR 沙箱'), None)
    if env is None:
        env = repository.create_environment(EnvironmentSpec(name='Airwallex PKR 沙箱',
            base_url='https://hapi-live-10.test.funnymamu.com', variables=deepcopy(DEFAULTS)))
    else:
        for key, value in DEFAULTS.items():
            env.variables.setdefault(key, value)
        repository.update_environment(env.id, env)
    flow = [saved_cases[PREFIX + 'flow/' + branch] for branch in ('success', 'failure')]
    groups = {'flow': ('Airwallex PKR - 提现审核回调全流程', flow),
              'success': ('Airwallex PKR - 成功流程', flow[:1]),
              'failure': ('Airwallex PKR - 失败退款流程', flow[1:]),
              'contract': ('Airwallex PKR - 接口与参数校验', [case_id for ref, case_id in saved_cases.items()
                          if '/flow/' not in ref and existing_enabled(ref, cases)])}
    existing_suites = {item.name: item for item in repository.list_suites(limit=10000)[0]}
    suites = {}
    for key, (name, case_ids) in groups.items():
        suite = SuiteSpec(name=name, description='详见 docs/testing/AIRWALLEX_PK.md。前置条件缺失时不发送提现。',
                          case_ids=case_ids, env_id=env.id, concurrency=1, failure_strategy='stop', tags=['airwallex', 'PKR'])
        current = existing_suites.get(name)
        saved = repository.update_suite(current.id, suite) if current else repository.create_suite(suite)
        suites[key] = saved.id
    return {'endpoints': len(endpoints), 'cases': len(cases), 'suite_ids': suites, 'env_id': env.id}


def existing_enabled(ref, cases):
    return next(case.enabled for case in cases if case.source_ref == ref)


def main():
    import argparse
    import json
    from ..config import load_config
    from ..repository import ProjectRegistry
    parser = argparse.ArgumentParser(description='同步 Airwallex PKR 接口、用例和套件（不执行提现）')
    parser.add_argument('--project', default='hapi-air')
    parser.add_argument('--data-dir')
    args = parser.parse_args()
    config = load_config(**({'data_dir': args.data_dir} if args.data_dir else {}))
    registry = ProjectRegistry(config)
    print(json.dumps(install(registry.repository(args.project)), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
