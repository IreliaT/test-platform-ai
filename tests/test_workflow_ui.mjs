import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { test } from 'node:test';
import { inheritedValue, parameterText, parseParameter, workflowParameters, workflowTimeoutSeconds } from '../hapitest/web/workflow.js';

const cases = JSON.parse(execFileSync('python3', ['-c',
  'import json; from hapitest.packs.airwallex_pk import definitions; print(json.dumps([c.to_dict() for c in definitions()[1]]))',
], { cwd: new URL('..', import.meta.url), encoding: 'utf8' }));

test('成功与退款流程只要求外部输入，自动提取值不进入表单', () => {
  for (const spec of cases.filter((item) => item.source_ref.includes('/flow/'))) {
    const inputs = workflowParameters(spec);
    assert.equal(spec.setup.length, 16);
    assert.equal(inputs.length, 15);
    assert.deepEqual(inputs.filter((item) => item.required).map((item) => item.name).sort(), spec.setup[0].required_variables.slice().sort());
    assert.equal(inputs.find((item) => item.name === 'sls_security_token').required, false);
    assert.equal(inputs.find((item) => item.name === 'beneficiary_data').kind, 'json-text');
    assert.equal(inputs.find((item) => item.name === 'user_token').secret, true);
    assert.equal(workflowTimeoutSeconds(spec), 765);
  }
});

test('提取前依赖是输入，同步骤提取后的断言以及后续引用是内部值', () => {
  const inputs = workflowParameters({ setup: [
    { request: { path: '/{id}', headers: [{ enabled: false, value: '${disabled}' }] },
      extractors: [{ name: 'id', expression: '$.id' }, { name: 'token', expression: '${pattern}' }],
      assertions: [{ type: 'variable', name: 'token', expected: '${optional:-fallback}' }] },
    { request: { path: '/{id}', headers: [{ value: '{{token}}' }, { value: '${optional?}' }, { value: '{{extra?}}' }] } },
  ], request: { path: '/done' }, assertions: [{ type: 'variable', name: 'token' }] });
  assert.deepEqual(inputs.map((item) => [item.name, item.required]), [['id', true], ['pattern', true], ['optional', false], ['extra', false]]);
});

test('支持两种变量语法、路径变量和可选变量的后续必填提升', () => {
  const inputs = workflowParameters({ setup: [{ request: { path: '/${a:-x}/{{b?}}/{c}' } }],
    request: { path: '/${a}/{{b}}' }, assertions: [{ enabled: false, expected: '${disabled}' }] });
  assert.deepEqual(inputs.map((item) => [item.name, item.required]), [['a', true], ['b', true], ['c', true]]);
});

test('用例值优先于环境，包含显式空值、0 和 false', () => {
  const spec = { variables: { value: '', number: 0, boolean: false } };
  const env = { variables: { value: 'environment', inherited: 'keep' } };
  assert.deepEqual(inheritedValue(spec, env, 'value'), { value: '', source: '用例' });
  assert.equal(parameterText(inheritedValue(spec, env, 'number').value), '0');
  assert.equal(parameterText(inheritedValue(spec, env, 'boolean').value), 'false');
  assert.deepEqual(inheritedValue(spec, env, 'inherited'), { value: 'keep', source: '环境' });
});

test('检查变量不存在或为空的断言允许留空', () => {
  const inputs = workflowParameters({ request: { path: '/ok' }, assertions: [
    { type: 'variable', name: 'absent', operator: 'not_exists' },
    { type: 'variable', name: 'empty', operator: 'is_empty' },
  ] });
  assert.deepEqual(inputs.map((item) => [item.name, item.required]), [['absent', false], ['empty', false]]);
});

test('收款资料校验 JSON 对象，发送时保持字符串；ID 保留大整数精度', () => {
  const input = { kind: 'json-text' };
  const text = '{"accountName":"Test Recipient","iban":"test"}';
  assert.equal(parseParameter(input, text), text);
  for (const invalid of ['[]', 'null', '123', '{']) assert.throws(() => parseParameter(input, invalid));
  assert.equal(parseParameter({ kind: 'integer' }, '123'), 123);
  assert.equal(parseParameter({ kind: 'integer' }, '9007199254740993'), '9007199254740993');
  assert.throws(() => parseParameter({ kind: 'integer' }, '1.5'));
  assert.throws(() => parseParameter({}, '******'));
  assert.deepEqual(parseParameter({}, '{"enabled":false}', { enabled: true }), { enabled: false });
  assert.throws(() => parseParameter({}, '[]', {}));
});
