/**
 * 用例管理：GET/POST /api/v1/projects/{pid}/cases
 * 完整用例编辑器（请求 / 断言 / 提取器）+ 单条运行 POST …/cases/{id}/run。
 */

import { Api } from '../api.js';
import { inheritedValue, parameterText, parseParameter, workflowParameters, workflowTimeoutSeconds } from '../workflow.js';
import {
  ASSERTION_TYPE_LABELS,
  EXTRACTOR_SOURCE_LABELS,
  OPERATOR_LABELS,
  authEditor,
  caseResultBlock,
  checkboxControl,
  clear,
  confirmDialog,
  el,
  emptyBlock,
  enumOf,
  errorBanner,
  formField,
  icon,
  itemsOf,
  kvEditor,
  loadingBlock,
  numberControl,
  runAction,
  selectControl,
  statusChip,
  textControl,
  textareaControl,
  toast,
  totalOf,
} from '../app.js';

const PAGE_SIZE = 100;

/** 各断言类型的推荐操作符（提示用；最终以后端校验器为准）。 */
const OPERATORS_BY_TYPE = {
  status_code: ['eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'in', 'not_in', 'between'],
  body_text: ['eq', 'ne', 'contains', 'not_contains', 'starts_with', 'ends_with', 'regex', 'is_empty', 'not_empty', 'length_eq', 'length_gt', 'length_lt'],
  json_path: ['eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'contains', 'not_contains', 'starts_with', 'ends_with', 'regex', 'in', 'not_in', 'exists', 'not_exists', 'is_true', 'is_false', 'is_empty', 'not_empty', 'type_is', 'length_eq', 'length_gt', 'length_lt', 'between', 'approx', 'json_contains', 'json_equals'],
  header: ['eq', 'ne', 'contains', 'not_contains', 'starts_with', 'ends_with', 'regex', 'exists', 'not_exists', 'is_empty', 'not_empty', 'length_eq', 'length_gt', 'length_lt'],
  cookie: ['eq', 'ne', 'contains', 'not_contains', 'starts_with', 'ends_with', 'regex', 'exists', 'not_exists', 'is_empty', 'not_empty', 'length_eq', 'length_gt', 'length_lt'],
  response_time: ['eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'between', 'approx'],
  json_schema: ['json_contains', 'json_equals', 'eq', 'exists'],
  variable: ['eq', 'ne', 'contains', 'not_contains', 'regex', 'in', 'not_in', 'exists', 'not_exists', 'is_true', 'is_false', 'is_empty', 'not_empty', 'type_is'],
};

const NEEDS_PATH = ['json_path', 'json_schema'];
const NEEDS_NAME = ['header', 'cookie', 'variable'];
const NO_EXPECTED = ['exists', 'not_exists', 'is_empty', 'not_empty', 'is_true', 'is_false'];
const ARRAY_EXPECTED = ['in', 'not_in', 'between'];

const PRIORITY_LABELS = { P0: 'P0 最高', P1: 'P1 高', P2: 'P2 中', P3: 'P3 低' };

function operatorOptions(type, current) {
  const recommended = OPERATORS_BY_TYPE[type] || enumOf('operators');
  const all = enumOf('operators');
  const list = recommended.slice();
  all.forEach((operator) => {
    if (list.indexOf(operator) === -1) list.push(operator);
  });
  if (current && list.indexOf(current) === -1) list.unshift(current);
  return list.map((value, index) => ({
    value,
    label:
      OPERATOR_LABELS[value] ||
      value,
    group: recommended.indexOf(value) === -1 ? 'other' : 'recommended',
    index,
  }));
}

function operatorHintText(type, operator) {
  const recommended = OPERATORS_BY_TYPE[type] || [];
  const labels = recommended.map((value) => OPERATOR_LABELS[value] || value).join('、');
  const parts = [];
  parts.push('推荐操作符：' + (labels || '（无）'));
  if (NEEDS_PATH.indexOf(type) >= 0) parts.push('必须填写 path（JSON 路径，如 $.data.id）');
  if (NEEDS_NAME.indexOf(type) >= 0) parts.push('必须填写 name（' + (type === 'variable' ? '变量名' : type === 'header' ? '响应头名' : 'Cookie 名') + '）');
  if (ARRAY_EXPECTED.indexOf(operator) >= 0) parts.push('该操作符的期望值必须是 JSON 数组');
  if (operator && recommended.indexOf(operator) === -1) parts.push('当前操作符不在推荐列表内，请确认后端是否接受');
  return parts.join('；');
}

function parseExpected(text) {
  const trimmed = String(text).trim();
  if (trimmed === '') return '';
  if (/^[[{]/.test(trimmed) || /^(true|false|null)$/.test(trimmed) || /^-?\d+(\.\d+)?$/.test(trimmed)) {
    try {
      return JSON.parse(trimmed);
    } catch (err) {
      return text;
    }
  }
  return text;
}

function expectedToText(value) {
  if (value === null || value === undefined) return '';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value);
  } catch (err) {
    return String(value);
  }
}

function emptyCase() {
  return {
    id: null,
    name: '',
    folder: '',
    description: '',
    endpoint_id: null,
    request: {
      method: 'GET',
      path: '/',
      base_url: null,
      query: [],
      headers: [],
      cookies: [],
      body_type: 'none',
      body: null,
      auth: { type: 'inherit', token: '', username: '', password: '', key: '', value: '', location: 'header', scheme: 'Bearer' },
      timeout_ms: null,
      follow_redirects: true,
      verify_tls: true,
    },
    assertions: [{ type: 'status_code', operator: 'eq', expected: 200, path: '', name: '', tolerance: null, message: '', enabled: true }],
    extractors: [],
    setup: [],
    variables: {},
    tags: [],
    priority: 'P2',
    enabled: true,
    retries: 0,
    source: 'manual',
    source_ref: '',
  };
}

export async function render(ctx) {
  const pid = ctx.pid;
  const view = {
    q: '',
    folder: '',
    priority: '',
    enabled: '',
    limit: PAGE_SIZE,
    offset: 0,
    items: [],
    total: 0,
    folders: { case_folders: [] },
    environments: [],
    current: null,
    dirty: false,
    running: false,
    lastResult: null,
    runValues: new Map(),
    runEnvId: '',
  };

  const main = ctx.container;
  clear(main);
  main.appendChild(
    el(
      'div',
      { class: 'page-head' },
      el('h1', { text: '用例管理' }),
      el('button', {
        type: 'button',
        class: 'btn btn-primary',
        on: { click: () => selectCase(emptyCase(), true) },
      }, icon('i-plus'), '新建用例'),
      el('div', { class: 'page-sub', text: '左侧筛选与选择用例，右侧编辑请求 / 断言 / 提取器，可直接运行单条用例。' })
    )
  );

  /* ------------------------------------------------------------ 左列 */

  const qInput = textControl({
    id: 'case-q',
    placeholder: '搜索名称 / 路径',
    on: { input: (event) => { view.q = event.target.value; } },
  });
  const folderFilter = selectControl([{ value: '', label: '全部目录' }], {
    id: 'case-filter-folder',
    on: { change: (event) => apply({ folder: event.target.value }) },
  });
  const priorityFilter = selectControl(
    [{ value: '', label: '全部优先级' }].concat(enumOf('priorities').map((value) => ({ value, label: PRIORITY_LABELS[value] || value }))),
    { id: 'case-filter-priority', on: { change: (event) => apply({ priority: event.target.value }) } }
  );
  const enabledFilter = selectControl(
    [
      { value: '', label: '全部状态' },
      { value: 'true', label: '仅启用' },
      { value: 'false', label: '仅停用' },
    ],
    { id: 'case-filter-enabled', on: { change: (event) => apply({ enabled: event.target.value }) } }
  );
  const searchForm = el(
    'form',
    { class: 'filter-bar', role: 'search', novalidate: true },
    formField('搜索', qInput, { id: 'case-q' }),
    formField('目录', folderFilter, { id: 'case-filter-folder' }),
    formField('优先级', priorityFilter, { id: 'case-filter-priority' }),
    formField('状态', enabledFilter, { id: 'case-filter-enabled' }),
    el('div', { class: 'row' }, el('button', { type: 'submit', class: 'btn' }, icon('i-search'), '查询'))
  );
  searchForm.addEventListener('submit', (event) => {
    event.preventDefault();
    apply({ q: qInput.value.trim() });
  });

  const listHost = el('div', { class: 'picker' });
  const listCard = el(
    'section',
    { class: 'card' },
    searchForm,
    listHost,
    el('div', { class: 'row', style: { padding: '8px 12px' } }, el('span', { class: 'muted', id: 'case-count' }))
  );

  const editorHost = el('section', { class: 'card' });
  main.appendChild(el('div', { class: 'split' }, listCard, editorHost));

  function apply(patch) {
    Object.assign(view, patch, { offset: 0 });
    loadList();
  }

  async function loadFolders() {
    const [ok, data] = await runAction(() => Api.listFolders(pid, { signal: ctx.signal }));
    if (ok && data) {
      view.folders = { case_folders: data.case_folders || [] };
      const current = folderFilter.value;
      clear(folderFilter);
      folderFilter.appendChild(el('option', { value: '', text: '全部目录' }));
      view.folders.case_folders.forEach((folder) => folderFilter.appendChild(el('option', { value: folder, text: folder })));
      folderFilter.value = view.folders.case_folders.indexOf(current) >= 0 ? current : '';
      view.folder = folderFilter.value;
    }
  }

  async function loadEnvironments() {
    const [ok, data] = await runAction(() => Api.listEnvironments(pid, { signal: ctx.signal }));
    view.environments = ok ? itemsOf(data) : [];
  }

  async function loadList() {
    clear(listHost);
    listHost.appendChild(loadingBlock('正在加载用例…'));
    let data;
    try {
      data = await Api.listCases(
        pid,
        {
          q: view.q,
          folder: view.folder,
          priority: view.priority,
          enabled: view.enabled,
          limit: view.limit,
          offset: view.offset,
        },
        { signal: ctx.signal }
      );
    } catch (err) {
      clear(listHost);
      listHost.appendChild(errorBanner(err));
      return;
    }
    if (ctx.signal.aborted) return;
    view.items = itemsOf(data);
    view.total = totalOf(data);
    renderList();
  }

  function renderList() {
    clear(listHost);
    const countNode = document.getElementById('case-count');
    if (countNode) countNode.textContent = '共 ' + view.total + ' 条用例';
    if (!view.items.length) {
      listHost.appendChild(emptyBlock('没有匹配的用例', '调整筛选条件，或点击「新建用例」。'));
      return;
    }
    const list = el('ul', { class: 'picker-list' });
    view.items.forEach((item) => {
      const current = view.current && view.current.id === item.id;
      const button = el(
        'button',
        {
          type: 'button',
          class: 'link-btn',
          'aria-current': current ? 'true' : null,
          style: { textAlign: 'left', width: '100%' },
          on: { click: () => openCaseFromList(item) },
        },
        el('span', { text: item.name || '(未命名用例)' }),
        el(
          'span',
          { class: 'row tight' },
          el('span', { class: 'chip', text: item.priority || 'P2' }),
          item.enabled === false ? el('span', { class: 'chip chip-skipped', text: '停用' }) : null,
          item.folder ? el('span', { class: 'tag', text: item.folder }) : null
        ),
        el('span', {
          class: 'faint mono',
          text: (item.request && item.request.method ? item.request.method : 'GET') + ' ' + ((item.request && item.request.path) || '/'),
        })
      );
      const li = el('li', {}, button);
      if (current) li.setAttribute('aria-selected', 'true');
      list.appendChild(li);
    });
    listHost.appendChild(list);
  }

  async function openCaseFromList(item) {
    if (view.dirty) {
      const confirmed = await confirmDialog({
        title: '放弃未保存的修改？',
        message: '当前用例有未保存的修改，切换后这些修改会丢失。',
        confirmLabel: '放弃修改并切换',
        danger: false,
      });
      if (!confirmed) return;
    }
    let spec = item;
    try {
      spec = await Api.getCase(pid, item.id, { signal: ctx.signal });
    } catch (err) {
      toast(err.message || '用例详情加载失败', 'error');
      return;
    }
    selectCase(spec, false);
  }

  /* ------------------------------------------------------------ 编辑器 */

  function markDirty() {
    view.dirty = true;
    const badge = document.getElementById('case-dirty');
    if (badge) badge.hidden = false;
  }

  /**
   * 只更新地址栏 hash，不触发路由重渲染（视图内切换用例用）。
   * 这样刷新页面仍会落到同一条用例。
   */
  function setHashSilently(hash) {
    try {
      window.history.replaceState(null, '', hash);
    } catch (err) {
      /* 忽略：不影响当前视图 */
    }
  }

  function selectCase(spec, isNew) {
    view.current = spec;
    view.dirty = false;
    view.lastResult = null;
    view.runValues = new Map();
    view.runEnvId = '';
    if (isNew) {
      setHashSilently('#/p/' + encodeURIComponent(pid) + '/cases');
    } else if (spec && spec.id) {
      setHashSilently('#/p/' + encodeURIComponent(pid) + '/cases/' + encodeURIComponent(spec.id));
    }
    renderEditor();
    renderList();
  }

  function renderEditor() {
    clear(editorHost);
    const spec = view.current;
    if (!spec) {
      editorHost.appendChild(emptyBlock('未选择用例', '从左侧选择一条用例，或点击「新建用例」。'));
      return;
    }
    const isNew = !spec.id;
    const request = spec.request || {};
    const model = {
      assertions: (spec.assertions || []).map((item) => Object.assign({ tolerance: null, message: '', enabled: true }, item)),
      extractors: (spec.extractors || []).map((item) => Object.assign({ scope: 'run', required: false }, item)),
      tags: (spec.tags || []).join(', '),
      variables: spec.variables || {},
      setup: spec.setup || [],
      source: spec.source || 'manual',
      source_ref: spec.source_ref || '',
      endpoint_id: spec.endpoint_id === undefined ? null : spec.endpoint_id,
    };
    const setupArea = textareaControl({
      id: 'case-setup', rows: 12, value: JSON.stringify(model.setup, null, 2),
      on: { input: () => { markDirty(); } },
    });
    const setupSummary = el('ol', { class: 'workflow-steps' });
    const stages = model.setup.concat([{ name: '最终检查：' + (spec.name || '主请求'), request,
      assertions: model.assertions, extractors: model.extractors }]);
    stages.forEach((item, index) => {
      const req = item.request || {};
      const final = index === model.setup.length;
      const details = el('details', { class: 'workflow-step' },
        el('summary', {},
          el('span', { class: 'workflow-number', text: final ? '✓' : String(index + 1) }),
          el('span', { class: 'workflow-step-heading' },
            el('strong', { text: item.name || '步骤 ' + (index + 1) }),
            el('span', { class: 'mono field-hint workflow-path', text: (req.method || 'GET') + ' ' + (req.path || '/') })
          ),
          el('span', { class: 'chip', text: final ? '最终检查' : item.poll_timeout_ms ? '等待 ' + item.poll_timeout_ms / 1000 + ' 秒' : '执行一次' })
        ),
        el('div', { class: 'workflow-step-body stack' },
          item.continue_on_failure ? el('div', { class: 'field-hint', text: '此步骤失败后允许继续执行。' }) : null,
          (item.required_variables || []).length ? el('div', { class: 'field-hint', text: '执行前需要：' + item.required_variables.join('、') }) : null,
          el('div', { class: 'subform-title', text: '请求配置（变量由下方运行参数填入）' }),
          el('pre', { class: 'workflow-code', text: JSON.stringify(req, null, 2) }),
          (item.extractors || []).length ? el('div', { class: 'stack' },
            el('div', { class: 'subform-title', text: '自动提取，供后续步骤使用' }),
            ...item.extractors.map((value) => el('div', { class: 'field-hint mono', text: value.name + ' ← ' + value.expression }))
          ) : null,
          el('div', { class: 'subform-title', text: '校验条件' }),
          el('ul', { class: 'workflow-checks' }, ...(item.assertions || []).filter((value) => value.enabled !== false).map((value) =>
            el('li', { text: (value.path || value.name || ASSERTION_TYPE_LABELS[value.type] || value.type) + ' ' +
              (OPERATOR_LABELS[value.operator || 'eq'] || value.operator || '等于') + ' ' + expectedToText(value.expected) })
          ))
        )
      );
      setupSummary.appendChild(el('li', {}, details));
    });
    const workflowBlock = el('fieldset', { class: 'subform stack' },
      el('legend', { text: '流程步骤（按顺序执行）' }),
      el('div', { class: 'row' },
        el('span', { class: 'field-hint', text: model.setup.length + ' 个流程步骤，随后执行最终请求与检查。点击步骤查看详情。' }),
        el('div', { class: 'spacer' }),
        el('button', { type: 'button', class: 'btn btn-sm', text: '展开全部', on: { click: () => {
          setupSummary.querySelectorAll('details').forEach((node) => { node.open = true; });
        } } }),
        el('button', { type: 'button', class: 'btn btn-sm', text: '收起全部', on: { click: () => {
          setupSummary.querySelectorAll('details').forEach((node) => { node.open = false; });
        } } })
      ),
      setupSummary,
      el('details', {}, el('summary', { text: '编辑流程 JSON' }),
        formField('步骤、轮询、提取器与断言', setupArea, { id: 'case-setup',
          hint: 'poll_timeout_ms 设置等待上限；仅只读查询可轮询。required 提取失败会停止后续步骤。' }))
    );

    const nameInput = textControl({
      id: 'case-name',
      value: spec.name || '',
      on: {
        input: (event) => {
          spec.name = event.target.value;
          markDirty();
        },
      },
    });
    const folderInput = textControl({
      id: 'case-folder',
      value: spec.folder || '',
      placeholder: '例如 用户中心',
      on: {
        input: (event) => {
          spec.folder = event.target.value;
          markDirty();
        },
      },
    });
    const prioritySelect = selectControl(
      enumOf('priorities').map((value) => ({ value, label: PRIORITY_LABELS[value] || value })),
      {
        id: 'case-priority',
        value: spec.priority || 'P2',
        on: {
          change: (event) => {
            spec.priority = event.target.value;
            markDirty();
          },
        },
      }
    );
    const enabledBox = checkboxControl({
      id: 'case-enabled',
      label: '启用（停用后运行记为跳过）',
      checked: spec.enabled !== false,
      on: {
        change: (event) => {
          spec.enabled = event.target.checked;
          markDirty();
        },
      },
    });
    const retriesInput = numberControl({
      id: 'case-retries',
      value: spec.retries || 0,
      min: 0,
      max: 10,
      on: {
        input: (event) => {
          spec.retries = Number(event.target.value || 0);
          markDirty();
        },
      },
    });
    const tagsInput = textControl({
      id: 'case-tags',
      value: model.tags,
      placeholder: '多个标签用英文逗号分隔',
      on: {
        input: (event) => {
          model.tags = event.target.value;
          markDirty();
        },
      },
    });
    const descriptionArea = textareaControl({
      id: 'case-description',
      rows: 3,
      value: spec.description || '',
      on: {
        input: (event) => {
          spec.description = event.target.value;
          markDirty();
        },
      },
    });

    /* -------- 请求 -------- */

    const methodSelect = selectControl(
      enumOf('http_methods').map((value) => ({ value, label: value })),
      {
        id: 'case-method',
        value: request.method || 'GET',
        on: {
          change: (event) => {
            request.method = event.target.value;
            markDirty();
          },
        },
      }
    );
    const pathInput = textControl({
      id: 'case-path',
      value: request.path || '/',
      placeholder: '/api/v1/users/{id}',
      on: {
        input: (event) => {
          request.path = event.target.value;
          markDirty();
        },
      },
    });
    const baseUrlInput = textControl({
      id: 'case-base-url',
      value: request.base_url || '',
      placeholder: '留空则使用环境 / 项目 base_url',
      on: {
        input: (event) => {
          request.base_url = event.target.value;
          markDirty();
        },
      },
    });
    const timeoutInput = numberControl({
      id: 'case-timeout',
      value: request.timeout_ms === null || request.timeout_ms === undefined ? '' : request.timeout_ms,
      min: 1,
      on: {
        input: (event) => {
          request.timeout_ms = event.target.value;
          markDirty();
        },
      },
    });
    const followBox = checkboxControl({
      id: 'case-follow',
      label: '跟随重定向',
      checked: request.follow_redirects !== false,
      on: {
        change: (event) => {
          request.follow_redirects = event.target.checked;
          markDirty();
        },
      },
    });
    const tlsBox = checkboxControl({
      id: 'case-verify-tls',
      label: '校验 TLS 证书',
      checked: request.verify_tls !== false,
      on: {
        change: (event) => {
          request.verify_tls = event.target.checked;
          markDirty();
        },
      },
    });

    const queryEditor = kvEditor({
      items: request.query,
      nameHeader: 'Query 名',
      valueHeader: 'Query 值',
      addLabel: '新增查询参数',
      hint: '仅启用的参数会拼接；值为空也会保留 ?k=',
    });
    const headerEditor = kvEditor({
      items: request.headers,
      nameHeader: 'Header 名',
      valueHeader: 'Header 值',
      addLabel: '新增请求头',
    });
    const cookieEditor = kvEditor({
      items: request.cookies,
      nameHeader: 'Cookie 名',
      valueHeader: 'Cookie 值',
      addLabel: '新增 Cookie',
    });

    const bodyArea = textareaControl({
      id: 'case-body',
      rows: 6,
      value:
        request.body === null || request.body === undefined
          ? ''
          : typeof request.body === 'string'
            ? request.body
            : JSON.stringify(request.body, null, 2),
      on: {
        input: (event) => {
          request.body = event.target.value;
          validateBody(false);
          markDirty();
        },
        blur: () => validateBody(true),
      },
    });
    const bodyHint = el('span', { class: 'field-hint' });
    const bodyTypeSelect = selectControl(
      enumOf('body_types').map((value) => ({ value, label: value })),
      {
        id: 'case-body-type',
        value: request.body_type || 'none',
        on: {
          change: (event) => {
            request.body_type = event.target.value;
            bodyArea.disabled = request.body_type === 'none';
            validateBody(true);
            markDirty();
          },
        },
      }
    );
    bodyArea.disabled = (request.body_type || 'none') === 'none';

    function validateBody(showMessage) {
      const type = request.body_type || 'none';
      const text = bodyArea.value;
      if (type !== 'json') {
        bodyArea.removeAttribute('aria-invalid');
        bodyHint.className = 'field-hint';
        bodyHint.textContent =
          type === 'none' ? 'body_type=none 时不发送请求体' : 'body_type=' + type + '：内容按原文发送';
        return true;
      }
      if (!text.trim()) {
        bodyArea.removeAttribute('aria-invalid');
        bodyHint.className = 'field-hint';
        bodyHint.textContent = 'JSON 请求体为空，将发送空 body';
        return true;
      }
      try {
        JSON.parse(text);
        bodyArea.removeAttribute('aria-invalid');
        bodyHint.className = 'field-hint';
        bodyHint.textContent = showMessage ? 'JSON 校验通过' : 'JSON 格式合法';
        return true;
      } catch (err) {
        bodyArea.setAttribute('aria-invalid', 'true');
        bodyHint.className = 'field-error';
        bodyHint.textContent = 'JSON 解析失败：' + err.message;
        return false;
      }
    }
    validateBody(false);

    const authBlock = authEditor(request.auth || {}, { legend: '认证（留空则继承环境）' });

    /* -------- 断言 -------- */

    const assertionsHost = el('div', { class: 'stack' });
    const assertionCount = el('span', { class: 'pill-count' });

    function refreshAssertionCount() {
      const enabled = model.assertions.filter((item) => item.enabled !== false).length;
      assertionCount.textContent = enabled + ' / ' + model.assertions.length;
    }

    function renderAssertions() {
      clear(assertionsHost);
      refreshAssertionCount();
      if (!model.assertions.length) {
        assertionsHost.appendChild(
          el('div', {
            class: 'field-hint',
            text: '尚未配置断言；无断言的用例会以 incomplete（缺条件未完成）结束，CI 视为失败。',
          })
        );
      }
      model.assertions.forEach((assertion, index) => {
        assertionsHost.appendChild(assertionRow(assertion, index));
      });
    }

    function assertionRow(assertion, index) {
      const enabled = checkboxControl({
        id: 'assertion-enabled-' + index,
        label: '启用',
        checked: assertion.enabled !== false,
        on: {
          change: (event) => {
            assertion.enabled = event.target.checked;
            refreshAssertionCount();
            markDirty();
          },
        },
      });
      const typeSelect = selectControl(
        enumOf('assertion_types').map((value) => ({ value, label: ASSERTION_TYPE_LABELS[value] || value })),
        {
          id: 'assertion-type-' + index,
          value: assertion.type || 'status_code',
          on: {
            change: (event) => {
              assertion.type = event.target.value;
              markDirty();
              renderAssertions();
            },
          },
        }
      );
      const operatorSelect = selectControl(
        operatorOptions(assertion.type || 'status_code', assertion.operator).map((option) => ({
          value: option.value,
          label: option.label,
        })),
        {
          id: 'assertion-operator-' + index,
          value: assertion.operator || 'eq',
          on: {
            change: (event) => {
              assertion.operator = event.target.value;
              hint.textContent = operatorHintText(assertion.type, assertion.operator);
              expectedInput.disabled = NO_EXPECTED.indexOf(assertion.operator) >= 0;
              markDirty();
            },
          },
        }
      );
      const expectedInput = textControl({
        id: 'assertion-expected-' + index,
        value: expectedToText(assertion.expected),
        placeholder: ARRAY_EXPECTED.indexOf(assertion.operator) >= 0 ? '[0, 100]' : '期望值',
        disabled: NO_EXPECTED.indexOf(assertion.operator) >= 0,
        on: {
          input: (event) => {
            assertion.expected = parseExpected(event.target.value);
            markDirty();
          },
        },
      });
      const pathInputRow = textControl({
        id: 'assertion-path-' + index,
        value: assertion.path || '',
        placeholder: '$.data.id（json_path / json_schema 必填）',
        on: {
          input: (event) => {
            assertion.path = event.target.value;
            markDirty();
          },
        },
      });
      const nameInputRow = textControl({
        id: 'assertion-name-' + index,
        value: assertion.name || '',
        placeholder: 'header / cookie / variable 名',
        on: {
          input: (event) => {
            assertion.name = event.target.value;
            markDirty();
          },
        },
      });
      const toleranceInput = numberControl({
        id: 'assertion-tolerance-' + index,
        value: assertion.tolerance === null || assertion.tolerance === undefined ? '' : assertion.tolerance,
        placeholder: '1e-6',
        on: {
          input: (event) => {
            const raw = event.target.value.trim();
            assertion.tolerance = raw ? Number(raw) : null;
            markDirty();
          },
        },
      });
      const messageInput = textControl({
        id: 'assertion-message-' + index,
        value: assertion.message || '',
        placeholder: '自定义失败说明（可选）',
        on: {
          input: (event) => {
            assertion.message = event.target.value;
            markDirty();
          },
        },
      });
      const hint = el('div', {
        class: 'field-hint',
        id: 'assertion-hint-' + index,
        text: operatorHintText(assertion.type, assertion.operator),
      });

      const move = (delta) => {
        const target = index + delta;
        if (target < 0 || target >= model.assertions.length) return;
        const tmp = model.assertions[target];
        model.assertions[target] = model.assertions[index];
        model.assertions[index] = tmp;
        markDirty();
        renderAssertions();
      };

      return el(
        'fieldset',
        { class: 'subform' },
        el(
          'legend',
          {},
          '断言 #' + (index + 1),
          ' ',
          statusChip(assertion.enabled === false ? 'skipped' : 'passed')
        ),
        el(
          'div',
          { class: 'stack' },
          el(
            'div',
            { class: 'row' },
            enabled.node,
            el('div', { class: 'spacer' }),
            el('button', {
              type: 'button',
              class: 'btn btn-sm',
              'aria-label': '上移断言 #' + (index + 1),
              disabled: index === 0,
              on: { click: () => move(-1) },
            }, icon('i-up', 'icon-sm')),
            el('button', {
              type: 'button',
              class: 'btn btn-sm',
              'aria-label': '下移断言 #' + (index + 1),
              disabled: index === model.assertions.length - 1,
              on: { click: () => move(1) },
            }, icon('i-down', 'icon-sm')),
            el('button', {
              type: 'button',
              class: 'btn btn-sm btn-danger',
              on: {
                click: () => {
                  model.assertions.splice(index, 1);
                  markDirty();
                  renderAssertions();
                },
              },
            }, icon('i-trash', 'icon-sm'), '删除')
          ),
          el(
            'div',
            { class: 'grid-2' },
            formField('断言类型 type', typeSelect, { id: 'assertion-type-' + index }),
            formField('操作符 operator', operatorSelect, { id: 'assertion-operator-' + index })
          ),
          el(
            'div',
            { class: 'grid-3' },
            formField('期望值 expected', expectedInput, { id: 'assertion-expected-' + index }),
            formField('JSON 路径 path', pathInputRow, { id: 'assertion-path-' + index }),
            formField('名称 name', nameInputRow, { id: 'assertion-name-' + index })
          ),
          el(
            'div',
            { class: 'grid-3' },
            formField('容差 tolerance（approx 用）', toleranceInput, { id: 'assertion-tolerance-' + index }),
            formField('自定义消息 message', messageInput, { id: 'assertion-message-' + index })
          ),
          hint
        )
      );
    }

    /* -------- 提取器 -------- */

    const extractorsHost = el('div', { class: 'stack' });

    function renderExtractors() {
      clear(extractorsHost);
      if (!model.extractors.length) {
        extractorsHost.appendChild(
          el('div', { class: 'field-hint', text: '尚未配置提取器；提取出的变量可供后续用例与断言引用。' })
        );
      }
      model.extractors.forEach((extractor, index) => {
        extractorsHost.appendChild(extractorRow(extractor, index));
      });
    }

    function extractorRow(extractor, index) {
      const nameInputRow = textControl({
        id: 'extractor-name-' + index,
        value: extractor.name || '',
        placeholder: '变量名，如 token',
        on: {
          input: (event) => {
            extractor.name = event.target.value;
            markDirty();
          },
        },
      });
      const sourceSelect = selectControl(
        enumOf('extractor_sources').map((value) => ({ value, label: EXTRACTOR_SOURCE_LABELS[value] || value })),
        {
          id: 'extractor-source-' + index,
          value: extractor.source || 'json_path',
          on: {
            change: (event) => {
              extractor.source = event.target.value;
              markDirty();
            },
          },
        }
      );
      const expressionInput = textControl({
        id: 'extractor-expression-' + index,
        value: extractor.expression || '',
        placeholder: '$.data.token / Set-Cookie 名 / 正则',
        on: {
          input: (event) => {
            extractor.expression = event.target.value;
            markDirty();
          },
        },
      });
      const defaultInput = textControl({
        id: 'extractor-default-' + index,
        value: extractor.default === null || extractor.default === undefined ? '' : String(extractor.default),
        placeholder: '未命中时的默认值',
        on: {
          input: (event) => {
            extractor.default = parseExpected(event.target.value);
            markDirty();
          },
        },
      });
      const scopeSelect = selectControl(
        [
          { value: 'run', label: 'run（本次运行内共享）' },
          { value: 'case', label: 'case（仅本用例）' },
        ],
        {
          id: 'extractor-scope-' + index,
          value: extractor.scope || 'run',
          on: {
            change: (event) => {
              extractor.scope = event.target.value;
              markDirty();
            },
          },
        }
      );
      const requiredBox = checkboxControl({
        id: 'extractor-required-' + index,
        label: '必填（未提取到则失败）',
        checked: Boolean(extractor.required),
        on: {
          change: (event) => {
            extractor.required = event.target.checked;
            markDirty();
          },
        },
      });
      return el(
        'fieldset',
        { class: 'subform' },
        el('legend', {}, '提取器 #' + (index + 1)),
        el(
          'div',
          { class: 'stack' },
          el(
            'div',
            { class: 'grid-2' },
            formField('变量名 name *', nameInputRow, { id: 'extractor-name-' + index }),
            formField('来源 source', sourceSelect, { id: 'extractor-source-' + index })
          ),
          el(
            'div',
            { class: 'grid-2' },
            formField('表达式 expression', expressionInput, { id: 'extractor-expression-' + index }),
            formField('默认值 default', defaultInput, { id: 'extractor-default-' + index })
          ),
          el(
            'div',
            { class: 'grid-2' },
            formField('作用域 scope', scopeSelect, { id: 'extractor-scope-' + index }),
            el('div', { class: 'field' }, el('span', { class: 'field-label', text: '选项' }), requiredBox.node)
          ),
          el(
            'div',
            { class: 'row' },
            el('div', { class: 'spacer' }),
            el('button', {
              type: 'button',
              class: 'btn btn-sm btn-danger',
              on: {
                click: () => {
                  model.extractors.splice(index, 1);
                  markDirty();
                  renderExtractors();
                },
              },
            }, icon('i-trash', 'icon-sm'), '删除提取器')
          )
        )
      );
    }

    /* -------- 运行面板 -------- */

    const parameters = workflowParameters(spec);
    const parameterFields = new Map();
    const parameterHost = el('fieldset', { class: 'workflow-parameters stack' });
    const parameterStatus = el('span', { class: 'chip', 'aria-live': 'polite' });
    const envSelect = selectControl(
      [{ value: '', label: '使用项目默认环境' }].concat(
        view.environments.map((env) => ({
          value: String(env.id),
          label: env.name + (env.is_default ? '（默认）' : ''),
        }))
      ),
      { id: 'case-run-env', value: view.runEnvId, on: { change: () => {
        view.runEnvId = envSelect.value;
        renderParameters();
      } } }
    );

    function selectedEnvironment() {
      return view.environments.find((env) => envSelect.value ? String(env.id) === envSelect.value : env.is_default) || {};
    }

    function parameterDefault(input) {
      return inheritedValue(spec, selectedEnvironment(), input.name);
    }

    function refreshParameterStatus() {
      const required = parameters.filter((input) => input.required);
      const configured = required.filter((input) => {
        const text = view.runValues.get(input.name);
        const value = text && text.trim() ? text.trim() : parameterDefault(input).value;
        return value !== null && value !== undefined && value !== '';
      }).length;
      parameterStatus.textContent = '已配置 ' + configured + ' / ' + required.length + ' 项必填参数';
      parameterStatus.className = 'chip ' + (configured === required.length ? 'chip-passed' : 'chip-incomplete');
    }

    function renderParameters() {
      clear(parameterHost);
      parameterFields.clear();
      if (!parameters.length) {
        parameterHost.hidden = true;
        return;
      }
      parameterHost.hidden = false;
      parameterHost.appendChild(el('legend', { text: '运行参数' }));
      parameterHost.appendChild(el('div', { class: 'field-hint', text: '填写值仅用于本次运行；留空继承用例或所选环境。Token / 密钥不会回显，也不会保存到用例或环境。' }));
      const groups = new Map();
      parameters.forEach((input, index) => {
        if (!groups.has(input.group)) {
          const grid = el('div', { class: 'workflow-input-grid' });
          groups.set(input.group, grid);
          parameterHost.appendChild(el('section', { class: 'stack' }, el('h3', { class: 'subform-title', text: input.group }), grid));
        }
        const inherited = parameterDefault(input);
        const hasInherited = inherited.value !== null && inherited.value !== undefined && inherited.value !== '';
        const id = 'case-run-variable-' + index;
        const error = el('span', { class: 'field-error', id: id + '-error', hidden: true });
        const options = {
          id, rows: 4, type: input.secret ? 'password' : 'text',
          value: view.runValues.has(input.name) ? view.runValues.get(input.name) :
            input.secret || inherited.value === '******' ? '' : parameterText(inherited.value),
          placeholder: hasInherited ? '留空继承' + inherited.source + '已配置的值' : input.placeholder || '请输入' + input.label,
          on: { input: (event) => {
            view.runValues.set(input.name, event.target.value);
            error.hidden = true;
            event.target.removeAttribute('aria-invalid');
            refreshParameterStatus();
          } },
        };
        const control = input.kind === 'json-text' || (!input.secret && typeof inherited.value === 'object' && inherited.value !== null)
          ? textareaControl(options) : textControl(options);
        control.setAttribute('aria-describedby', id + '-hint ' + id + '-error');
        if (input.kind === 'integer') control.setAttribute('inputmode', 'numeric');
        const field = formField(input.label + (input.required ? ' *' : '（选填）'), control, { id });
        field.classList.add('workflow-input');
        field.appendChild(el('span', { class: 'mono field-hint', text: input.name }));
        field.appendChild(el('span', { class: 'field-hint', id: id + '-hint', text:
          (hasInherited ? inherited.source + '已配置，留空可继承。' : '尚未配置。') + (input.hint || '') }));
        field.appendChild(error);
        groups.get(input.group).appendChild(field);
        parameterFields.set(input.name, { control, error });
      });
      refreshParameterStatus();
    }

    function runVariables() {
      const variables = Object.create(null);
      let firstInvalid = null;
      parameters.forEach((input) => {
        const { control, error } = parameterFields.get(input.name);
        error.hidden = true;
        control.removeAttribute('aria-invalid');
        const inherited = parameterDefault(input).value;
        const override = (view.runValues.get(input.name) || '').trim();
        const effective = override || parameterText(inherited);
        try {
          if (!effective && input.required) throw new Error('请填写' + input.label + '，或选择已配置该参数的环境');
          if (override) variables[input.name] = parseParameter(input, override, inherited);
          else if (effective && input.kind === 'json-text') variables[input.name] = parseParameter(input, effective, inherited);
        } catch (err) {
          error.textContent = err.message;
          error.hidden = false;
          control.setAttribute('aria-invalid', 'true');
          if (!firstInvalid) firstInvalid = control;
        }
      });
      if (firstInvalid) {
        firstInvalid.focus();
        throw new Error('请补齐或修正运行参数');
      }
      return variables;
    }
    renderParameters();

    const runButton = el(
      'button',
      {
        type: 'button',
        class: 'btn btn-primary',
        on: {
          click: async () => {
            if (view.running) return;
            if (!spec.id) {
              toast('请先保存用例再运行', 'error');
              return;
            }
            if (view.dirty) {
              toast('请先保存用例修改再运行', 'error');
              return;
            }
            let payload;
            try { payload = { variables: runVariables(), timeout_s: workflowTimeoutSeconds(spec) }; } catch (err) {
              toast(err.message, 'error');
              return;
            }
            if (envSelect.value) payload.env_id = Number(envSelect.value);
            view.running = true;
            runButton.disabled = true;
            envSelect.disabled = true;
            parameterHost.disabled = true;
            const stop = el('span', { class: 'loading-indicator' }, el('span', { class: 'spinner', 'aria-hidden': 'true' }), '执行中…');
            resultHost.appendChild(stop);
            const [ok, data] = await runAction(() => Api.runCase(pid, spec.id, payload));
            view.running = false;
            runButton.disabled = false;
            envSelect.disabled = false;
            parameterHost.disabled = false;
            if (stop.parentNode) stop.parentNode.removeChild(stop);
            if (!ok || ctx.signal.aborted || !view.current || view.current.id !== spec.id) return;
            const result = (data && (data.result || data.case_result)) || data || {};
            view.lastResult = result;
            renderResult();
          },
        },
      },
      icon('i-play'),
      '运行此用例'
    );

    const resultHost = el('div', { class: 'stack' });

    function renderResult() {
      clear(resultHost);
      if (!view.lastResult) {
        resultHost.appendChild(
          el('div', { class: 'field-hint', text: '运行结果将显示在这里（同步执行，无需轮询）。' })
        );
        return;
      }
      resultHost.appendChild(caseResultBlock(view.lastResult, { showName: false }));
    }

    const runPanel = el('fieldset', { class: 'subform stack' },
      el('legend', { text: '运行此用例' }),
      el('div', { class: 'row' },
        el('div', { style: { minWidth: '240px' } }, formField('环境', envSelect, { id: 'case-run-env' })),
        parameters.length ? parameterStatus : null
      ),
      parameterHost,
      el('div', { class: 'row' }, runButton,
        spec.source ? el('span', { class: 'faint workflow-path', text: '来源：' + spec.source + (spec.source_ref ? ' / ' + spec.source_ref : '') }) : null),
      el('div', { class: 'field-hint', text: '按顺序执行已保存的用例，运行结果与步骤详情会保存到「运行与历史」。' }),
      resultHost
    );

    /* -------- 顶部操作 -------- */

    const dirtyBadge = el('span', { class: 'chip chip-incomplete', id: 'case-dirty', text: '未保存', hidden: true });

    const saveButton = el(
      'button',
      {
        type: 'button',
        class: 'btn btn-primary',
        on: {
          click: async () => {
            let payload;
            try {
              payload = buildPayload();
            } catch (err) {
              toast(err.message, 'error');
              return;
            }
            const [ok, data] = await runAction(
              () => (isNew ? Api.createCase(pid, payload) : Api.updateCase(pid, spec.id, payload)),
              isNew ? '用例已创建' : '用例已保存'
            );
            if (!ok) return;
            view.dirty = false;
            const saved = (data && (data.case || data.case_spec)) || data;
            if (isNew) {
              await loadList();
              if (saved && saved.id) selectCase(saved, false);
              else selectCase(emptyCase(), true);
            } else {
              view.current = Object.assign({}, spec, saved && saved.id ? saved : {});
              renderEditor();
              await loadList();
            }
          },
        },
      },
      icon('i-check'),
      isNew ? '创建用例' : '保存'
    );

    function buildPayload() {
      try {
        model.setup = JSON.parse(setupArea.value || '[]');
        if (!Array.isArray(model.setup)) throw new Error('必须是数组');
      } catch (err) {
        throw new Error('流程步骤 setup 不是合法 JSON 数组：' + err.message);
      }
      const name = nameInput.value.trim();
      if (!name) throw new Error('用例名称不能为空');
      const path = pathInput.value.trim();
      if (!path) throw new Error('请求路径不能为空');
      const bodyType = request.body_type || 'none';
      let body = null;
      if (bodyType === 'json') {
        const text = bodyArea.value.trim();
        if (text) {
          try {
            body = JSON.parse(text);
          } catch (err) {
            throw new Error('请求体不是合法 JSON：' + err.message);
          }
        }
      } else if (bodyType !== 'none') {
        body = bodyArea.value;
      }

      const assertions = model.assertions.map((assertion) => {
        const type = assertion.type || 'status_code';
        const operator = assertion.operator || 'eq';
        const item = {
          type,
          operator,
          expected: NO_EXPECTED.indexOf(operator) >= 0 ? null : assertion.expected,
          path: assertion.path || '',
          name: assertion.name || '',
          tolerance: assertion.tolerance === undefined ? null : assertion.tolerance,
          message: assertion.message || '',
          enabled: assertion.enabled !== false,
        };
        if (NEEDS_PATH.indexOf(type) >= 0 && !item.path) {
          throw new Error('断言类型 ' + type + ' 必须填写 JSON 路径 path');
        }
        if (NEEDS_NAME.indexOf(type) >= 0 && !item.name) {
          throw new Error('断言类型 ' + type + ' 必须填写 name');
        }
        if (ARRAY_EXPECTED.indexOf(operator) >= 0) {
          if (!Array.isArray(item.expected)) {
            throw new Error('操作符 ' + operator + ' 的期望值必须是 JSON 数组');
          }
          if (operator === 'between' && item.expected.length !== 2) {
            throw new Error('操作符 between 的期望值必须是 [min, max]');
          }
        }
        if (NO_EXPECTED.indexOf(operator) < 0 && operator !== 'type_is' && (item.expected === '' || item.expected === null)) {
          throw new Error('操作符 ' + operator + ' 需要填写期望值 expected');
        }
        return item;
      });

      const extractors = model.extractors.map((extractor, index) => {
        if (!extractor.name || !String(extractor.name).trim()) {
          throw new Error('第 ' + (index + 1) + ' 个提取器缺少变量名');
        }
        return {
          name: String(extractor.name).trim(),
          source: extractor.source || 'json_path',
          expression: extractor.expression || '',
          default: extractor.default === '' ? null : extractor.default,
          scope: extractor.scope || 'run',
          required: Boolean(extractor.required),
        };
      });

      const timeoutRaw = timeoutInput.value.trim();
      return {
        name,
        folder: folderInput.value.trim(),
        description: descriptionArea.value,
        endpoint_id: model.endpoint_id,
        request: {
          method: methodSelect.value,
          path,
          base_url: baseUrlInput.value.trim() || null,
          query: queryEditor.getItems(),
          headers: headerEditor.getItems(),
          cookies: cookieEditor.getItems(),
          body_type: bodyType,
          body,
          auth: authBlock.getValue(),
          timeout_ms: timeoutRaw ? Number(timeoutRaw) : null,
          follow_redirects: followBox.input.checked,
          verify_tls: tlsBox.input.checked,
        },
        assertions,
        extractors,
        setup: model.setup,
        variables: model.variables,
        tags: model.tags
          .split(',')
          .map((item) => item.trim())
          .filter(Boolean),
        priority: prioritySelect.value,
        enabled: enabledBox.input.checked,
        retries: Number(retriesInput.value || 0),
        source: model.source,
        source_ref: model.source_ref,
      };
    }

    /* -------- 组装 -------- */

    const head = el(
      'div',
      { class: 'card-head' },
      el('h2', { text: isNew ? '新建用例' : '编辑用例 ' + (spec.id ? '#' + spec.id : '') }),
      dirtyBadge,
      el('div', { class: 'spacer' }),
      isNew
        ? null
        : el(
            'button',
            {
              type: 'button',
              class: 'btn btn-sm',
              on: {
                click: async () => {
                  const [ok, data] = await runAction(() => Api.duplicateCase(pid, spec.id, {}), '用例已复制');
                  if (!ok) return;
                  await loadList();
                  const created = (data && (data.case || data.case_spec)) || data;
                  if (created && created.id) selectCase(created, false);
                },
              },
            },
            icon('i-copy', 'icon-sm'),
            '复制'
          ),
      isNew
        ? null
        : el(
            'button',
            {
              type: 'button',
              class: 'btn btn-sm btn-danger',
              on: {
                click: async () => {
                  const confirmed = await confirmDialog({
                    title: '删除用例',
                    message: '将删除用例「' + (spec.name || spec.id) + '」，不可恢复。',
                    confirmLabel: '删除用例',
                  });
                  if (!confirmed) return;
                  const [ok] = await runAction(() => Api.deleteCase(pid, spec.id), '用例已删除');
                  if (ok) {
                    view.dirty = false;
                    view.current = null;
                    setHashSilently('#/p/' + encodeURIComponent(pid) + '/cases');
                    await loadList();
                    renderEditor();
                  }
                },
              },
            },
            icon('i-trash', 'icon-sm'),
            '删除'
          ),
      saveButton
    );
    dirtyBadge.hidden = !view.dirty;

    const body = el(
      'div',
      { class: 'card-body stack' },
      el(
        'div',
        { class: 'grid-2' },
        formField('用例名称 *', nameInput, { id: 'case-name' }),
        formField('目录 folder', folderInput, { id: 'case-folder' })
      ),
      el(
        'div',
        { class: 'grid-3' },
        formField('优先级 priority', prioritySelect, { id: 'case-priority' }),
        formField('重试次数 retries', retriesInput, { id: 'case-retries', hint: '失败后重试次数，0 表示不重试' }),
        el('div', { class: 'field' }, el('span', { class: 'field-label', text: '启用状态' }), enabledBox.node)
      ),
      formField('标签 tags', tagsInput, { id: 'case-tags' }),
      formField('描述 description', descriptionArea, { id: 'case-description' }),
      el(
        'fieldset',
        { class: 'subform' },
        el('legend', { text: '请求' }),
        el(
          'div',
          { class: 'stack' },
          el(
            'div',
            { class: 'grid-3' },
            formField('方法', methodSelect, { id: 'case-method' }),
            formField('路径 *', pathInput, { id: 'case-path' }),
            formField('base_url 覆盖', baseUrlInput, { id: 'case-base-url' })
          ),
          el(
            'div',
            { class: 'grid-2' },
            formField('超时 timeout_ms', timeoutInput, { id: 'case-timeout' }),
            el('div', { class: 'field' }, el('span', { class: 'field-label', text: '选项' }), el('div', { class: 'row' }, followBox.node, tlsBox.node))
          ),
          el('div', { class: 'subform-title' }, '查询参数 query'),
          queryEditor.node,
          el('div', { class: 'subform-title' }, '请求头 headers'),
          headerEditor.node,
          el('div', { class: 'subform-title' }, 'Cookie'),
          cookieEditor.node,
          el(
            'div',
            { class: 'grid-2' },
            formField('请求体类型 body_type', bodyTypeSelect, { id: 'case-body-type' }),
            formField('请求体 body（JSON 失焦校验）', bodyArea, { id: 'case-body' })
          ),
          bodyHint,
          authBlock.node
        )
      ),
      el(
        'fieldset',
        { class: 'subform' },
        el(
          'legend',
          {},
          '断言 assertions ',
          assertionCount
        ),
        el(
          'div',
          { class: 'stack' },
          assertionsHost,
          el(
            'div',
            { class: 'row' },
            el('button', {
              type: 'button',
              class: 'btn btn-sm',
              on: {
                click: () => {
                  model.assertions.push({
                    type: 'status_code',
                    operator: 'eq',
                    expected: 200,
                    path: '',
                    name: '',
                    tolerance: null,
                    message: '',
                    enabled: true,
                  });
                  markDirty();
                  renderAssertions();
                },
              },
            }, icon('i-plus', 'icon-sm'), '新增断言')
          )
        )
      ),
      workflowBlock,
      el(
        'fieldset',
        { class: 'subform' },
        el('legend', { text: '提取器 extractors' }),
        el(
          'div',
          { class: 'stack' },
          extractorsHost,
          el(
            'div',
            { class: 'row' },
            el('button', {
              type: 'button',
              class: 'btn btn-sm',
              on: {
                click: () => {
                  model.extractors.push({ name: '', source: 'json_path', expression: '', default: null, scope: 'run', required: false });
                  markDirty();
                  renderExtractors();
                },
              },
            }, icon('i-plus', 'icon-sm'), '新增提取器')
          )
        )
      ),
      runPanel
    );

    if (model.setup.length) {
      const advanced = el('details', { class: 'workflow-advanced' },
        el('summary', { text: '高级配置：最终请求、断言与提取器' }),
        el('div', { class: 'stack' })
      );
      Array.from(body.children).filter((node) => node.tagName === 'FIELDSET' && node !== workflowBlock && node !== runPanel)
        .forEach((node) => advanced.lastChild.appendChild(node));
      body.appendChild(advanced);
    }

    editorHost.appendChild(head);
    editorHost.appendChild(body);
    renderAssertions();
    renderExtractors();
    renderResult();
  }

  /* ------------------------------------------------------------ 启动 */

  await loadFolders();
  await loadEnvironments();

  if (ctx.arg) {
    try {
      const spec = await Api.getCase(pid, ctx.arg, { signal: ctx.signal });
      view.current = spec;
    } catch (err) {
      main.insertBefore(errorBanner(err), main.firstChild ? main.firstChild.nextSibling : null);
      view.current = null;
    }
  }

  await loadList();
  renderEditor();
}
