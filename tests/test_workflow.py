"""通过执行器公开接口验证异步业务流程，所有 HTTP 均为本机服务。"""
import json
import unittest

from hapitest.models import CaseSpec, EnvironmentSpec, RequestSpec
from hapitest.http_client import prepare
from hapitest.repository import ProjectRegistry
from hapitest.runner import CaseRunner
from tests.support import LocalHttpServer, TempConfig


class WorkflowTest(unittest.TestCase):
    def test_missing_credentials_stops_entire_suite_before_any_request(self):
        from hapitest.packs.airwallex_pk import install
        from hapitest.runner import RunManager, RunRequest
        with TempConfig() as config, LocalHttpServer() as server:
            registry = ProjectRegistry(config)
            registry.create(name='Hapi', project_id='hapi-air', base_url=server.base_url)
            repo = registry.repository('hapi-air')
            pack = install(repo)
            environment = repo.get_environment(pack['env_id'])
            environment.base_url = server.base_url
            repo.update_environment(environment.id, environment)
            run = RunManager(config, registry).run_sync(RunRequest(project_id='hapi-air', suite_id=pack['suite_ids']['flow']))
            self.assertEqual('incomplete', run.status)
            self.assertEqual(1, len(run.case_results))
            self.assertIn('sls_access_key_secret', run.case_results[0].error)
            self.assertEqual([], server.records)

    def test_log_timeout_never_uses_a_stale_transfer_id_or_submits_callback(self):
        def handler(method, path, headers, body):
            return 200, {'Content-Type': 'application/json'}, b'{"msg":"id=ffffffff-ffff-ffff-ffff-ffffffffffff, requestId=awx-withdraw-337,"}'

        with TempConfig() as config, LocalHttpServer(handler) as server:
            registry = ProjectRegistry(config)
            registry.create(name='日志', project_id='flow')
            case = CaseSpec.from_dict({'name': '缺少本订单 ID', 'request': {'method': 'POST', 'path': '/simulate'},
                'assertions': [dict(type='status_code', expected=200)],
                'setup': [{'name': '查订单 338 的日志', 'request': {'path': '/logs'},
                           'poll_timeout_ms': 35, 'poll_interval_ms': 10,
                           'extractors': [{'name': 'transfer_id', 'source': 'regex', 'required': True,
                               'expression': r'id=([0-9a-f-]{36}),\s*requestId=awx-withdraw-338(?=,)'}]}]})
            result = CaseRunner(config, registry.repository('flow')).run_case(case, EnvironmentSpec(base_url=server.base_url),
                {'transfer_id': 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'})
            self.assertEqual('incomplete', result.status)
            self.assertGreater(result.steps[0]['attempts'], 1)
            self.assertTrue(all(item['path'] == '/logs' for item in server.records))

    def test_polling_cannot_repeat_a_write_without_explicit_read_only_marker(self):
        with TempConfig() as config, LocalHttpServer() as server:
            registry = ProjectRegistry(config)
            registry.create(name='流程', project_id='flow')
            case = CaseSpec.from_dict({'name': '禁止重放提现', 'request': {'path': '/ok'},
                'assertions': [dict(type='status_code', expected=200)],
                'setup': [{'name': '申请提现', 'request': {'method': 'POST', 'path': '/withdraw'},
                           'poll_timeout_ms': 1000, 'assertions': [dict(type='status_code', expected=200)]}]})
            result = CaseRunner(config, registry.repository('flow')).run_case(case, EnvironmentSpec(base_url=server.base_url))
            self.assertEqual('incomplete', result.status)
            self.assertEqual([], server.records)

    def test_installed_pack_runs_both_branches_and_refunds_original_usd(self):
        from hapitest.packs.airwallex_pk import install
        from hapitest.runner import RunManager, RunRequest
        wallet, orders, channel_states = [100], [], {}
        log_queries = [0]
        settlement = {}

        def handler(method, path, headers, body):
            route = path.split('?')[0]
            data = json.loads(body) if body else {}
            response = {"code": 200}
            if route == '/fun-wallet/withdrawInfo':
                response['data'] = {"currency": "USD", "money": '%.2f' % wallet[0]}
            elif route == '/fun-wallet/calculateActualAmount':
                response['data'] = {"currency": "PKR", "money": "2785", "handlingFees": "0", "handlingFeesCurrency": "PKR"}
            elif route == '/fun-wallet/withdraw':
                self.assertEqual('0-PK-airwallex-airwallex:bank_transfer-PKR', data['channelProductId'])
                self.assertIsInstance(data['data'], str)
                wallet[0] -= 10
                orders.insert(0, {'id': 338 + len(orders), 'uid': 'fixture-user', 'state': 0, 'currency': 'PKR',
                                  'money': 10, 'reallyMoney': 2785, 'shouldPayMoney': 2785, 'handlingFees': 0})
            elif route == '/admin/log/common/list':
                selected = [item for item in orders if not data.get('id') or item['id'] == data['id']]
                if data.get('id') in settlement:
                    pending = settlement[data['id']]
                    pending[1] += 1
                    if pending[1] >= 2:
                        selected[0]['state'] = 2 if pending[0] == 'PAID' else 4
                        if pending[0] == 'FAILED':
                            wallet[0] += 10
                        del settlement[data['id']]
                response['data'] = {'items': selected, 'total': len(selected)}
            elif route == '/admin/log/common/update-fields':
                order = next(item for item in orders if item['id'] == data['id'])
                self.assertEqual(0, order['state'])
                self.assertEqual(0, data['filters']['state'])
                order['state'] = data['fields']['state']
            elif route == '/admin/payout/bathWithdraw':
                order = next(item for item in orders if item['id'] == data['withdrawOrderIds'][0])
                self.assertEqual(1, order['state'])
                order['state'] = 6
                channel_states[str(order['id'])] = 'PROCESSING' if order['id'] == 338 else 'SCHEDULED'
            elif route.startswith('/logstores/'):
                self.assertTrue(headers['Authorization'].startswith('LOG fake-id:'))
                log_queries[0] += 1
                oid = orders[0]['id']
                response = {'meta': {'progress': 'Complete'}, 'data': []}
                if log_queries[0] % 2 == 0:
                    response['data'] = [{'msg': 'id=ffffffff-ffff-ffff-ffff-ffffffffffff, requestId=awx-withdraw-1,'},
                                        {'msg': 'CreateTransferResult(id=00000000-0000-0000-0000-000000000%s, requestId=awx-withdraw-%s, status=SCHEDULED)' % (oid, oid)}]
            elif route == '/api/v1/authentication/login':
                return 201, {'Content-Type': 'application/json'}, b'{"token":"fake-access-token"}'
            elif route.startswith('/api/v1/transfers/'):
                oid = route.rsplit('/', 1)[-1][-3:]
                response = {'id': route.rsplit('/', 1)[-1], 'request_id': 'awx-withdraw-' + oid,
                            'transfer_currency': 'PKR', 'source_currency': 'USD', 'transfer_method': 'LOCAL',
                            'transfer_amount': 2785, 'status': channel_states[oid]}
            elif route.startswith('/api/v1/simulation/transfers/'):
                oid = route.split('/')[-2][-3:]
                self.assertEqual('Bearer fake-access-token', headers.get('Authorization'))
                self.assertEqual('2025-11-11', headers.get('x-api-version'))
                next_status = data['next_status']
                if next_status == 'PROCESSING' and channel_states[oid] == 'PROCESSING':
                    return 400, {'Content-Type': 'application/json'}, b'{"code":"operation_failed","message":"Request could not be processed"}'
                allowed = {'SCHEDULED': 'PROCESSING', 'PROCESSING': 'SENT'}
                self.assertTrue(next_status == allowed.get(channel_states[oid]) or
                                channel_states[oid] == 'SENT' and next_status in ('PAID', 'FAILED'))
                channel_states[oid] = next_status
                if next_status in ('PAID', 'FAILED'):
                    order = next(item for item in orders if str(item['id']) == oid)
                    order['state'] = 6 if next_status == 'PAID' else 3
                    settlement[order['id']] = [next_status, 0]
                response = {'status': next_status}
            else:
                return 404, {}, b'unknown route'
            return 200, {'Content-Type': 'application/json'}, json.dumps(response).encode()

        with TempConfig() as config, LocalHttpServer(handler) as server:
            registry = ProjectRegistry(config)
            registry.create(name='Hapi', project_id='hapi-air', base_url=server.base_url)
            repo = registry.repository('hapi-air')
            summary = install(repo)
            again = install(repo)
            self.assertEqual(summary['suite_ids'], again['suite_ids'])
            environment = repo.get_environment(summary['env_id'])
            environment.base_url = server.base_url
            environment.variables.update({'user_token': 'fake-user-token', 'admin_token': 'fake-admin-token',
                'test_uid': 'fixture-user', 'withdraw_tier_id': 'fixture-10', 'airwallex_client_id': 'fake-client',
                'airwallex_api_key': 'fake-api-key', 'sls_access_key_id': 'fake-id', 'sls_access_key_secret': 'fake-secret',
                'airwallex_base_url': server.base_url, 'airwallex_auth_base_url': server.base_url, 'sls_base_url': server.base_url})
            repo.update_environment(environment.id, environment)
            # 本机 fixture 上缩短日志等待，实际套件默认等待 SLS 入库。
            for case in repo.list_cases(limit=1000)[0]:
                for step in case.setup:
                    if step.poll_timeout_ms:
                        step.poll_timeout_ms, step.poll_interval_ms = 1000, 10
                repo.update_case(case.id, case)
            manager = RunManager(config, registry)
            run = manager.run_sync(RunRequest(project_id='hapi-air', suite_id=summary['suite_ids']['flow']))
            self.assertEqual('passed', run.status, [(item.error, item.steps[-1] if item.steps else {}) for item in run.case_results])
            self.assertEqual([4, 2], [item['state'] for item in orders])
            self.assertEqual(90, wallet[0])
            self.assertNotIn('fake-access-token', json.dumps(run.to_dict()))

    def test_sls_query_is_signed_and_credentials_are_masked(self):
        request = RequestSpec.from_dict({
            "method": "POST", "path": "/logstores/demo/logs", "body_type": "json", "body": {},
            "headers": [{"name": "Content-Type", "value": "application/json"},
                        {"name": "Date", "value": "Mon, 12 Oct 2026 09:00:00 GMT"}],
            "auth": {"type": "aliyun_sls", "username": "fake-id", "password": "fake-secret"},
        })
        compiled = prepare(request, 'https://demo.ap-southeast-1.log.aliyuncs.com')
        # 已知输入的独立签名向量：POST、空 JSON、固定日期、SLS 0.6.0。
        self.assertEqual('LOG fake-id:MRhNf8ZZuTHr3XtzEiAjanAUM0U=', compiled.headers.get('Authorization'))
        self.assertEqual('******', compiled.preview['headers']['Authorization'])
        self.assertNotIn('fake-secret', json.dumps(compiled.preview))

    def test_waits_for_required_id_then_uses_it_and_dynamic_assertion(self):
        seen = []

        def handler(method, path, headers, body):
            seen.append(path)
            payload = ({"data": {}} if seen.count('/logs') == 1 else
                       {"data": {"id": 338}}) if path == '/logs' else {"id": 338}
            return 200, {"Content-Type": "application/json"}, json.dumps(payload).encode()

        with TempConfig() as config, LocalHttpServer(handler) as server:
            registry = ProjectRegistry(config)
            registry.create(name="流程", project_id="flow")
            runner = CaseRunner(config, registry.repository("flow"))
            case = CaseSpec.from_dict({
                "name": "等待查到渠道 ID", "request": {"path": "/order/${order_id}"},
                "assertions": [{"type": "json_path", "path": "$.id", "expected": "${order_id}"}],
                "setup": [{"name": "查日志", "request": {"path": "/logs"},
                           "poll_timeout_ms": 1000, "poll_interval_ms": 10,
                           "extractors": [{"name": "order_id", "expression": "$.data.id", "required": True}]}],
            })
            saved = registry.repository("flow").create_case(case)
            result = runner.run_case(saved, EnvironmentSpec(base_url=server.base_url))
            self.assertEqual("passed", result.status, result.to_dict())
            self.assertIn('/order/338', seen)
            self.assertEqual(2, result.steps[0]['attempts'])


if __name__ == '__main__':
    unittest.main()
