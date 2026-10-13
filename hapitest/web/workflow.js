/** 从执行顺序识别用户输入，排除前序步骤提取值和执行器内置变量。 */
const INTERNAL_VARIABLES = new Set(['_case_started_epoch', '_now_epoch']);

const PARAMETER_INFO = {
  user_token: { label: '测试用户 Token', group: '测试账号与提现', secret: true, hint: '用于用户接口请求头 token。' },
  admin_token: { label: '管理员 Token', group: '测试账号与提现', secret: true, hint: '需具备审核与 PayoutAdmin 权限。' },
  test_uid: { label: '测试用户 UID', group: '测试账号与提现', kind: 'integer', hint: '与用户 Token 对应的同一专用测试账号。' },
  withdraw_tier_id: { label: '提现档位 ID', group: '测试账号与提现', kind: 'integer', hint: 'withdrawInfo 返回的 withdrawId，不是提现订单 ID。' },
  beneficiary_data: { label: '收款资料', group: '测试账号与提现', kind: 'json-text', hint: '填写 JSON 对象文本，发送时保留为 JSON 字符串。', placeholder: '{"accountName":"Test Recipient","iban":"PK36SCBL0000001123456702"}' },
  channel_product_id: { label: '渠道产品 ID', group: '测试账号与提现', hint: '巴基斯坦 PKR 渠道使用 0-PK-airwallex-airwallex:bank_transfer-PKR。' },
  airwallex_client_id: { label: 'Airwallex Client ID', group: 'Airwallex 沙箱', hint: '当前沙箱账号的 Client ID。' },
  airwallex_api_key: { label: 'Airwallex API Key', group: 'Airwallex 沙箱', secret: true, hint: '用于运行时获取渠道 Token。' },
  airwallex_base_url: { label: '渠道查询与模拟地址', group: 'Airwallex 沙箱', hint: '沙箱地址：https://api.sandbox.airwallex.com' },
  airwallex_auth_base_url: { label: '渠道认证地址', group: 'Airwallex 沙箱', secret: false, hint: '沙箱地址：https://api-demo.airwallex.com' },
  sls_base_url: { label: 'SLS 服务地址', group: 'SLS 日志', hint: '日志 Project 对应的完整服务地址。' },
  sls_logstore: { label: 'SLS Logstore', group: 'SLS 日志', hint: '保存渠道创建订单日志的 Logstore。' },
  sls_access_key_id: { label: 'SLS AccessKey ID', group: 'SLS 日志', secret: true, hint: '需有目标 Logstore 的读取权限。' },
  sls_access_key_secret: { label: 'SLS AccessKey Secret', group: 'SLS 日志', secret: true },
  sls_security_token: { label: 'SLS STS Token', group: 'SLS 日志', secret: true, hint: '使用 STS 时填写，否则留空。' },
};

function references(value, visit) {
  if (typeof value === 'string') {
    const pattern = /\$\{([A-Za-z_][A-Za-z0-9_.-]*)(?::-[^{}]*)?(\?)?\}|\{\{([A-Za-z_][A-Za-z0-9_.-]*)(?::-[^{}]*)?(\?)?\}\}/g;
    for (const match of value.matchAll(pattern)) {
      visit(match[1] || match[3], !match[0].includes(':-') && !(match[2] || match[4]));
    }
  } else if (Array.isArray(value)) {
    value.forEach((item) => references(item, visit));
  } else if (value && typeof value === 'object' && value.enabled !== false) {
    Object.values(value).forEach((item) => references(item, visit));
  }
}

export function workflowParameters(spec) {
  const available = new Set(INTERNAL_VARIABLES);
  const inputs = new Map();
  function add(name, required, step) {
    if (!name || available.has(name)) return;
    if (!inputs.has(name)) inputs.set(name, { name, required: false, steps: new Set() });
    const input = inputs.get(name);
    input.required = input.required || required;
    input.steps.add(step);
  }
  const stages = (spec.setup || []).concat([{
    name: '最终请求', request: spec.request, assertions: spec.assertions, extractors: spec.extractors,
  }]);
  stages.forEach((stage, index) => {
    const visit = (name, required) => add(name, required, index + 1);
    (stage.required_variables || []).forEach((name) => visit(name, true));
    references(stage.request, visit);
    const path = stage.request && stage.request.path || '';
    // RequestSpec.path 也接受单花括号路径变量。
    for (const match of path.matchAll(/(?<![$\{])\{([A-Za-z_][A-Za-z0-9_.-]*)\}(?!\})/g)) visit(match[1], true);
    (stage.extractors || []).forEach((item) => references(item.expression, visit));
    // 前置步骤先提取后断言；最终请求先断言后提取。
    if (index < stages.length - 1) (stage.extractors || []).forEach((item) => available.add(item.name));
    (stage.assertions || []).filter((item) => item.enabled !== false).forEach((item) => {
      references(item, visit);
      if (item.type === 'variable') visit(item.name, !['not_exists', 'is_empty'].includes(item.operator));
    });
  });
  return Array.from(inputs.values()).map((input) => Object.assign({
    label: input.name, group: '其他运行参数',
    secret: /password|passwd|pwd|secret|token|access_key|accesskey|api_key|apikey|auth|cookie|session|credential|signature|sign|salt|private_key|privatekey/i.test(input.name),
  }, Object.hasOwn(PARAMETER_INFO, input.name) ? PARAMETER_INFO[input.name] : {}, input, { steps: Array.from(input.steps) }));
}

export function inheritedValue(spec, environment, name) {
  const variables = spec.variables || {};
  if (Object.hasOwn(variables, name)) return { value: variables[name], source: '用例' };
  return { value: (environment.variables || {})[name], source: '环境' };
}

export function parameterText(value) {
  if (value === null || value === undefined) return '';
  return typeof value === 'string' ? value : JSON.stringify(value);
}

export function workflowTimeoutSeconds(spec) {
  const stages = (spec.setup || []).concat([spec]);
  return Math.max(120, 30 + stages.reduce((total, stage) => total +
    Math.min(300000, Math.max(0, Number(stage.poll_timeout_ms) || 0)) / 1000 +
    (Number(stage.request && stage.request.timeout_ms) || 15000) / 1000, 0));
}

export function parseParameter(input, text, inherited) {
  if (text === '******') throw new Error('请填写实际值，不能使用掩码 ******');
  if (input.kind === 'json-text') {
    let value;
    try { value = JSON.parse(text); } catch (err) { throw new Error('请填写合法的 JSON 对象'); }
    if (!value || Array.isArray(value) || typeof value !== 'object') throw new Error('请填写 JSON 对象');
    return text;
  }
  if (input.kind === 'integer') {
    if (!/^\d+$/.test(text)) throw new Error('请填写非负整数 ID');
    const value = Number(text);
    return Number.isSafeInteger(value) ? value : text;
  }
  if (inherited !== null && inherited !== undefined && typeof inherited !== 'string') {
    let value;
    try { value = JSON.parse(text); } catch (err) { throw new Error('请填写与原值类型一致的 JSON 值'); }
    if (typeof value !== typeof inherited || Array.isArray(value) !== Array.isArray(inherited) || value === null) {
      throw new Error('请填写与原值类型一致的 JSON 值');
    }
    return value;
  }
  return text;
}
