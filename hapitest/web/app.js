/**
 * HapiTest Web 控制台 —— 路由与应用外壳。
 *
 * 视图模块通过 dynamic import 懒加载，避免与外壳形成循环依赖；
 * 每个视图导出一个 `render(ctx)` 函数（见下方 Ctx 说明）。
 */

import { Api, ApiError } from './api.js';

/* ============================================================ 安全 HTML */

/** 已转义片段：只有 html``/raw() 能产出，用于 innerHTML 白名单。 */
export class SafeHtml {
  constructor(value) {
    this.value = String(value);
  }
  toString() {
    return this.value;
  }
}

/** 显式标记为可信片段（仅用于代码内写死的字面量）。 */
export function raw(value) {
  return new SafeHtml(value);
}

/** 转义任意值，供 innerHTML / 属性拼接使用。 */
export function escapeHtml(value) {
  if (value === null || value === undefined) return '';
  return String(value).replace(/[&<>"']/g, (ch) => {
    if (ch === '&') return '&amp;';
    if (ch === '<') return '&lt;';
    if (ch === '>') return '&gt;';
    if (ch === '"') return '&quot;';
    return '&#39;';
  });
}

function renderFragmentValue(value) {
  if (value === null || value === undefined || value === false || value === true) return '';
  if (value instanceof SafeHtml) return value.value;
  if (Array.isArray(value)) return value.map(renderFragmentValue).join('');
  return escapeHtml(value);
}

/**
 * 标签模板：所有插值默认转义，SafeHtml/数组按已转义内容拼接。
 * 用法：`html`<td>${caseItem.name}</td>``
 */
export function html(strings) {
  const values = Array.prototype.slice.call(arguments, 1);
  let out = '';
  for (let i = 0; i < strings.length; i += 1) {
    out += strings[i];
    if (i < values.length) out += renderFragmentValue(values[i]);
  }
  return new SafeHtml(out);
}

/* ============================================================ DOM 工具 */

function appendChildren(node, children) {
  children.forEach((child) => {
    if (child === null || child === undefined || child === false || child === true) return;
    if (Array.isArray(child)) return appendChildren(node, child);
    if (child instanceof SafeHtml) {
      const holder = document.createElement('div');
      holder.innerHTML = child.value;
      while (holder.firstChild) node.appendChild(holder.firstChild);
      return;
    }
    if (child instanceof Node) {
      node.appendChild(child);
      return;
    }
    node.appendChild(document.createTextNode(String(child)));
  });
}

/**
 * 创建元素。
 * props 支持：class / text（textContent）/ html（只接受 html`` 结果）/ on（事件表）/
 * dataset / style（对象）/ 其余作为属性或 DOM 属性写入。
 */
export function el(tag, props, ...children) {
  const node = document.createElement(tag);
  const options = props || {};
  Object.keys(options).forEach((key) => {
    const value = options[key];
    if (value === null || value === undefined || value === false) return;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value === null || value === undefined ? '' : String(value);
    else if (key === 'html') {
      if (!(value instanceof SafeHtml)) {
        throw new TypeError('el(): html 只能接受 html`` / raw() 的返回值');
      }
      node.innerHTML = value.value;
    } else if (key === 'on') {
      Object.keys(value).forEach((eventName) => node.addEventListener(eventName, value[eventName]));
    } else if (key === 'dataset') {
      Object.assign(node.dataset, value);
    } else if (key === 'style' && typeof value === 'object') {
      Object.assign(node.style, value);
    } else if (key === 'value' || key === 'checked' || key === 'disabled' || key === 'selected') {
      node[key] = value;
    } else if (value === true) {
      node.setAttribute(key, '');
    } else {
      node.setAttribute(key, String(value));
    }
  });
  appendChildren(node, children);
  return node;
}

/**
 * 用已转义片段填充容器。
 * - SafeHtml：按标记写入 innerHTML（内容已在 html`` 中转义）；
 * - 其它值（Node / 字符串 / 数组）：清空后作为子节点追加，字符串按纯文本处理。
 */
export function mount(container, fragment) {
  clear(container);
  if (fragment instanceof SafeHtml) {
    container.innerHTML = fragment.value;
    return container;
  }
  appendChildren(container, [fragment]);
  return container;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

/** 图标：引用本地 sprite /assets/icons.svg（装饰性，无障碍标签由调用方提供）。 */
export function icon(name, extraClass) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'icon' + (extraClass ? ' ' + extraClass : ''));
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', '/assets/icons.svg#' + name);
  svg.appendChild(use);
  return svg;
}

/** 图标按钮（图标 + aria-label，键盘可达）。 */
export function iconButton(name, label, onClick, extraClass) {
  return el(
    'button',
    {
      type: 'button',
      class: 'btn btn-ghost btn-icon' + (extraClass ? ' ' + extraClass : ''),
      'aria-label': label,
      title: label,
      on: { click: onClick },
    },
    icon(name)
  );
}

/* ============================================================ 格式化 */

const CASE_STATUS_LABELS = {
  passed: '通过',
  failed: '失败',
  error: '错误',
  incomplete: '未完成',
  skipped: '跳过',
};

const RUN_STATUS_LABELS = {
  queued: '排队中',
  running: '运行中',
  passed: '通过',
  failed: '失败',
  error: '错误',
  incomplete: '未完成',
  canceled: '已取消',
};

export const FALLBACK_ENUMS = {
  http_methods: ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'],
  body_types: ['none', 'json', 'form', 'multipart', 'raw'],
  auth_types: ['none', 'inherit', 'bearer', 'basic', 'api_key', 'header', 'aliyun_sls'],
  assertion_types: ['status_code', 'body_text', 'json_path', 'header', 'cookie', 'response_time', 'json_schema', 'variable'],
  operators: [
    'eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'contains', 'not_contains', 'starts_with', 'ends_with',
    'regex', 'in', 'not_in', 'exists', 'not_exists', 'is_true', 'is_false', 'is_empty', 'not_empty',
    'type_is', 'length_eq', 'length_gt', 'length_lt', 'between', 'approx', 'json_contains', 'json_equals',
  ],
  priorities: ['P0', 'P1', 'P2', 'P3'],
  case_statuses: ['passed', 'failed', 'error', 'incomplete', 'skipped'],
  run_statuses: ['queued', 'running', 'passed', 'failed', 'error', 'incomplete', 'canceled'],
  import_sources: ['auto', 'openapi', 'swagger2', 'postman', 'apifox', 'curl', 'spring'],
  extractor_sources: ['json_path', 'json_path_all', 'header', 'cookie', 'regex', 'body_text', 'status_code', 'duration_ms'],
};

export const ASSERTION_TYPE_LABELS = {
  status_code: 'HTTP 状态码',
  body_text: '响应文本',
  json_path: 'JSON 路径',
  header: '响应头',
  cookie: 'Cookie',
  response_time: '响应耗时',
  json_schema: 'JSON Schema',
  variable: '变量',
};

export const OPERATOR_LABELS = {
  eq: '等于 ==',
  ne: '不等于 !=',
  gt: '大于 >',
  gte: '大于等于 >=',
  lt: '小于 <',
  lte: '小于等于 <=',
  contains: '包含',
  not_contains: '不包含',
  starts_with: '以…开头',
  ends_with: '以…结尾',
  regex: '正则匹配',
  in: '属于（数组）',
  not_in: '不属于（数组）',
  exists: '存在',
  not_exists: '不存在',
  is_true: '为真',
  is_false: '为假',
  is_empty: '为空',
  not_empty: '非空',
  type_is: '类型是',
  length_eq: '长度等于',
  length_gt: '长度大于',
  length_lt: '长度小于',
  between: '介于 [min,max]',
  approx: '约等于',
  json_contains: 'JSON 子集包含',
  json_equals: 'JSON 结构相等',
};

export const EXTRACTOR_SOURCE_LABELS = {
  json_path: 'JSON 路径',
  json_path_all: 'JSON 路径全部匹配（数组）',
  header: '响应头',
  cookie: 'Cookie',
  regex: '正则表达式',
  body_text: '响应文本',
  status_code: '状态码',
  duration_ms: '耗时(ms)',
};

export function statusLabel(status) {
  if (!status) return '未知';
  return CASE_STATUS_LABELS[status] || RUN_STATUS_LABELS[status] || String(status);
}

/** 状态徽标；incomplete（缺条件未完成）与 skipped 视觉上必须可区分。 */
export function statusChip(status) {
  const cls = 'chip chip-' + String(status || 'unknown').replace(/[^a-z_]/gi, '');
  return html`<span class="${cls}" title="${status || ''}">${statusLabel(status)}</span>`;
}

/** 若 meta 提供了状态枚举则使用它，否则退回内置常量。 */
export function statusOptions(kind) {
  const meta = state.meta;
  const key = kind === 'run' ? 'run_statuses' : 'case_statuses';
  const list = meta && Array.isArray(meta[key]) && meta[key].length ? meta[key] : FALLBACK_ENUMS[key];
  return list.map((value) => ({ value, label: statusLabel(value) }));
}

export function formatDuration(ms) {
  const value = Number(ms) || 0;
  if (value < 1000) return value + ' ms';
  if (value < 60000) return (value / 1000).toFixed(value < 10000 ? 2 : 1) + ' s';
  const minutes = Math.floor(value / 60000);
  const seconds = Math.round((value % 60000) / 1000);
  return minutes + ' 分 ' + seconds + ' 秒';
}

export function formatDateTime(iso) {
  if (!iso) return '—';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso);
  const pad = (num) => String(num).padStart(2, '0');
  return (
    date.getFullYear() +
    '-' + pad(date.getMonth() + 1) +
    '-' + pad(date.getDate()) +
    ' ' + pad(date.getHours()) +
    ':' + pad(date.getMinutes()) +
    ':' + pad(date.getSeconds())
  );
}

export function formatDate(iso) {
  if (!iso) return '—';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso);
  const pad = (num) => String(num).padStart(2, '0');
  return date.getFullYear() + '-' + pad(date.getMonth() + 1) + '-' + pad(date.getDate());
}

/** pass_rate 为 0~1 的比例（HTTP_API.md §2）。 */
export function formatPassRate(ratio) {
  if (ratio === null || ratio === undefined || ratio === '') return '—';
  const value = Number(ratio);
  if (Number.isNaN(value)) return '—';
  const percent = value <= 1 ? value * 100 : value;
  return percent.toFixed(percent >= 99.95 || percent === 0 ? 0 : 1) + '%';
}

export function formatBytes(size) {
  const value = Number(size) || 0;
  if (value < 1024) return value + ' B';
  if (value < 1024 * 1024) return (value / 1024).toFixed(1) + ' KB';
  return (value / 1024 / 1024).toFixed(2) + ' MB';
}

export function formatJson(value) {
  if (value === null || value === undefined) return '';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch (err) {
    return String(value);
  }
}

export function formatValue(value) {
  if (value === null) return 'null';
  if (value === undefined) return '—';
  if (typeof value === 'object') return formatJson(value);
  if (value === '') return '（空）';
  return String(value);
}

export function truncate(text, length) {
  const value = text === null || text === undefined ? '' : String(text);
  const limit = length || 160;
  return value.length > limit ? value.slice(0, limit) + '…' : value;
}

/** 数组/信封式列表统一取 items。 */
export function itemsOf(data) {
  if (Array.isArray(data)) return data;
  if (data && Array.isArray(data.items)) return data.items;
  return [];
}

export function totalOf(data) {
  if (data && typeof data.total === 'number') return data.total;
  return itemsOf(data).length;
}

/* ============================================================ 全局状态 */

export const state = {
  meta: null,
  health: null,
  projects: [],
  currentProject: null,
  lastProjectId: null,
  // 顶栏环境切换器用：当前项目的环境列表 + 它属于哪个项目（用于失效缓存）
  environments: [],
  environmentsPid: null,
};

const META_CACHE_KEY = 'hapitest.meta';
const LAST_PROJECT_KEY = 'hapitest.lastProject';

export function readLastProjectId() {
  try {
    return window.localStorage.getItem(LAST_PROJECT_KEY) || '';
  } catch (err) {
    return '';
  }
}

export function rememberProjectId(pid) {
  state.lastProjectId = pid || '';
  try {
    if (pid) window.localStorage.setItem(LAST_PROJECT_KEY, pid);
  } catch (err) {
    /* 隐私模式下忽略 */
  }
}

/** 取枚举字典（GET /api/v1/meta），会话内缓存一次。 */
export async function loadMeta(force) {
  if (state.meta && !force) return state.meta;
  try {
    const data = await Api.meta();
    state.meta = data && typeof data === 'object' ? data : {};
  } catch (err) {
    state.meta = Object.assign({}, FALLBACK_ENUMS);
  }
  return state.meta;
}

export function enumOf(key) {
  const meta = state.meta;
  const value = meta && Array.isArray(meta[key]) ? meta[key] : null;
  if (value && value.length) return value.slice();
  return (FALLBACK_ENUMS[key] || []).slice();
}

export async function loadHealth() {
  try {
    state.health = await Api.health();
  } catch (err) {
    state.health = null;
  }
  return state.health;
}

/* ============================================================ 提示与加载 */

export function toast(message, type = 'success', timeoutMs = 4200) {
  const host = document.getElementById('toasts');
  if (!host) return;
  let timer = null;
  const node = el('div', { class: 'toast toast-' + type, role: type === 'error' ? 'alert' : 'status' });
  const remove = () => {
    if (timer) clearTimeout(timer);
    if (node.parentNode) node.parentNode.removeChild(node);
  };
  node.appendChild(el('span', { class: 'toast-text', text: String(message) }));
  node.appendChild(
    el('button', { type: 'button', class: 'toast-close', 'aria-label': '关闭提示', on: { click: remove } }, '×')
  );
  host.appendChild(node);
  if (timeoutMs > 0) timer = setTimeout(remove, timeoutMs);
  return remove;
}

export function setLoading(on) {
  const node = document.getElementById('global-loading');
  if (node) node.hidden = !on;
}

/** 统一的异步动作包装：错误转 toast，返回 [ok, data]。 */
export async function runAction(action, successMessage) {
  try {
    const data = await action();
    if (successMessage) toast(successMessage, 'success');
    return [true, data];
  } catch (err) {
    if (err && err.name === 'AbortError') return [false, null];
    const message = err instanceof ApiError ? err.message : (err && err.message) || '操作失败';
    toast(message, 'error', 6000);
    return [false, null];
  }
}

export function errorBanner(err) {
  const message = err instanceof ApiError ? err.message : (err && err.message) || '加载失败';
  return el('div', { class: 'banner banner-error', role: 'alert' }, icon('i-alert'), el('span', { text: message }));
}

export function loadingBlock(text) {
  return el(
    'div',
    { class: 'loading-block', role: 'status' },
    el('span', { class: 'spinner', 'aria-hidden': 'true' }),
    text || '加载中…'
  );
}

export function emptyBlock(title, hint) {
  return el(
    'div',
    { class: 'empty' },
    el('div', { class: 'empty-title', text: title }),
    hint ? el('div', { text: hint }) : null
  );
}

/* ============================================================ 弹层 */

const overlayStack = [];

function focusableIn(root) {
  return Array.prototype.filter.call(
    root.querySelectorAll(
      'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    ),
    (node) => node.offsetParent !== null || node === document.activeElement
  );
}

/**
 * 打开抽屉或模态。
 * @param {'drawer'|'modal'} kind
 * @param {{title:string, content?:Node|SafeHtml, footer?:Node|Node[], wide?:boolean, onClose?:Function}} options
 */
export function openOverlay(kind, options = {}) {
  const root = document.getElementById('overlay-root');
  const previousFocus = document.activeElement;
  const overlay = el('div', { class: 'overlay overlay-' + kind + (options.wide ? ' panel-wide' : '') });
  const titleId = 'overlay-title-' + overlayStack.length + '-' + Date.now();
  const titleNode = el('h2', { id: titleId, text: options.title || '' });
  const closeButton = iconButton('i-close', '关闭', () => handle.close());
  const head = el('div', { class: 'panel-head' }, titleNode, closeButton);
  const body = el('div', { class: 'panel-body' });
  const foot = el('div', { class: 'panel-foot', hidden: true });
  const panel = el('div', { class: 'panel', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': titleId }, head, body, foot);
  overlay.appendChild(panel);

  const handle = {
    kind,
    root: overlay,
    panel,
    body,
    foot,
    setTitle(text) {
      titleNode.textContent = text;
    },
    setFooter(nodes) {
      clear(foot);
      const list = Array.isArray(nodes) ? nodes : nodes ? [nodes] : [];
      const visible = list.filter(Boolean);
      visible.forEach((node) => foot.appendChild(node));
      foot.hidden = visible.length === 0;
    },
    close() {
      const index = overlayStack.indexOf(handle);
      if (index === -1) return;
      overlayStack.splice(index, 1);
      if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
      if (previousFocus && typeof previousFocus.focus === 'function' && document.contains(previousFocus)) {
        previousFocus.focus();
      }
      if (typeof options.onClose === 'function') options.onClose();
    },
  };

  if (options.content) {
    appendChildren(body, [options.content]);
  }
  if (options.footer) handle.setFooter(options.footer);

  overlay.addEventListener('mousedown', (event) => {
    if (event.target === overlay) handle.close();
  });

  panel.addEventListener('keydown', (event) => {
    if (event.key !== 'Tab') return;
    const nodes = focusableIn(panel);
    if (!nodes.length) return;
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });

  root.appendChild(overlay);
  overlayStack.push(handle);

  const initial = focusableIn(panel).filter((node) => node !== closeButton);
  (initial[0] || closeButton).focus();
  return handle;
}

export function openDrawer(options) {
  return openOverlay('drawer', options);
}

export function openModal(options) {
  return openOverlay('modal', options);
}

/** 关闭最上层弹层；返回是否关闭了内容。 */
export function closeTopOverlay() {
  const handle = overlayStack[overlayStack.length - 1];
  if (!handle) return false;
  handle.close();
  return true;
}

export function closeAllOverlays() {
  while (overlayStack.length) overlayStack[overlayStack.length - 1].close();
}

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && overlayStack.length) {
    event.preventDefault();
    closeTopOverlay();
  }
});

/**
 * 危险操作确认框；requireText 非空时必须逐字输入才可提交。
 * @returns {Promise<boolean>}
 */
export function confirmDialog(options = {}) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    const input = options.requireText
      ? el('input', { type: 'text', id: 'confirm-input', autocomplete: 'off', spellcheck: 'false' })
      : null;
    const content = el(
      'div',
      { class: 'stack' },
      el('p', { text: options.message || '确认执行该操作？' }),
      options.requireText
        ? el(
            'div',
            { class: 'field' },
            el('label', { for: 'confirm-input' }, '请输入 ', el('code', { text: options.requireText }), ' 以确认：'),
            input
          )
        : null,
      options.hint ? el('div', { class: 'field-hint', text: options.hint }) : null
    );
    const submit = el('button', {
      type: 'button',
      class: options.danger === false ? 'btn btn-primary' : 'btn btn-danger',
      text: options.confirmLabel || '确认删除',
      disabled: Boolean(options.requireText),
      on: {
        click: () => {
          finish(true);
          handle.close();
        },
      },
    });
    if (input) {
      input.addEventListener('input', () => {
        submit.disabled = input.value.trim() !== options.requireText;
      });
    }
    const handle = openModal({
      title: options.title || '确认操作',
      content,
      footer: [
        el('div', { class: 'spacer' }),
        el('button', {
          type: 'button',
          class: 'btn',
          text: '取消',
          on: {
            click: () => {
              finish(false);
              handle.close();
            },
          },
        }),
        submit,
      ],
      onClose: () => finish(false),
    });
  });
}

/* ============================================================ 表单构件 */

/** 后端掩码值：写回时表示“保持原值不变”（HTTP_API.md §6）。 */
export const MASK_VALUE = '******';

export function formField(labelText, control, options = {}) {
  const node = el('div', { class: 'field' });
  if (labelText) node.appendChild(el('label', options.id ? { for: options.id } : {}, labelText));
  if (control) node.appendChild(control);
  if (options.hint) node.appendChild(el('span', { class: 'field-hint', text: options.hint }));
  return node;
}

export function textControl(props = {}) {
  return el('input', {
    type: props.type || 'text',
    id: props.id,
    value: props.value === undefined || props.value === null ? '' : String(props.value),
    placeholder: props.placeholder,
    autocomplete: props.autocomplete || 'off',
    spellcheck: props.spellcheck === undefined ? 'false' : props.spellcheck,
    'aria-label': props.ariaLabel,
    disabled: props.disabled,
    readonly: props.readonly,
    min: props.min,
    max: props.max,
    on: props.on || {},
  });
}

export function numberControl(props = {}) {
  return textControl(Object.assign({}, props, { type: 'number' }));
}

export function textareaControl(props = {}) {
  const node = el('textarea', {
    id: props.id,
    rows: props.rows || 6,
    placeholder: props.placeholder,
    spellcheck: 'false',
    'aria-label': props.ariaLabel,
    on: props.on || {},
  });
  node.value = props.value === undefined || props.value === null ? '' : String(props.value);
  return node;
}

export function selectControl(options = [], props = {}) {
  const node = el('select', {
    id: props.id,
    'aria-label': props.ariaLabel,
    disabled: props.disabled,
    on: props.on || {},
  });
  options.forEach((option) => {
    node.appendChild(
      el('option', {
        value: option.value,
        text: option.label,
        selected: String(option.value) === String(props.value === undefined ? '' : props.value),
      })
    );
  });
  return node;
}

export function checkboxControl(props = {}) {
  const input = el('input', {
    type: 'checkbox',
    id: props.id,
    checked: Boolean(props.checked),
    on: props.on || {},
  });
  const label = el('label', { class: 'check', for: props.id }, input, el('span', { text: props.label || '' }));
  return { node: label, input };
}

export function enumSelect(key, props = {}) {
  const values = Array.isArray(props.values) ? props.values : enumOf(key);
  const labels = props.labels || {};
  return selectControl(
    values.map((value) => ({ value, label: labels[value] || value })),
    props
  );
}

/** 掩码值输入框：显示“保持原值”，未修改时原样回传掩码。 */
export function maskedControl(props = {}) {
  const wasMasked = props.value === MASK_VALUE;
  const input = textControl({
    id: props.id,
    type: props.secret ? 'password' : 'text',
    value: wasMasked ? '' : props.value,
    placeholder: wasMasked ? '保持原值' : props.placeholder,
    'aria-label': props.ariaLabel,
    on: props.on,
  });
  let touched = !wasMasked;
  input.addEventListener('input', () => {
    touched = true;
  });
  return {
    node: input,
    input,
    wasMasked,
    /** 读取写回值：未改动且原本是掩码 → 回传掩码。 */
    read() {
      if (wasMasked && !touched) return MASK_VALUE;
      return input.value;
    },
  };
}

/**
 * query / header / cookie / form 的键值编辑器（KeyValue 列表）。
 * @param {{items?:Array, addLabel?:string, withDescription?:boolean,
 *          namePlaceholder?:string, valuePlaceholder?:string, hint?:string,
 *          keepEmptyName?:boolean, onChange?:Function}} options
 */
export function kvEditor(options = {}) {
  const items = (options.items || []).map((item) => ({
    name: item && item.name ? String(item.name) : '',
    value: item && item.value !== undefined && item.value !== null ? String(item.value) : '',
    enabled: !(item && item.enabled === false),
    description: (item && item.description) || '',
    required: Boolean(item && item.required),
  }));

  const tbody = el('tbody');
  const table = el(
    'table',
    { class: 'kv-table' },
    el(
      'thead',
      {},
      el(
        'tr',
        {},
        el('th', { class: 'kv-enable', text: '启用' }),
        el('th', { text: options.nameHeader || '名称' }),
        el('th', { text: options.valueHeader || '值' }),
        options.withDescription ? el('th', { text: '说明' }) : null,
        el('th', { class: 'kv-actions', text: '操作' })
      )
    ),
    tbody
  );

  function renderRows(focusLast) {
    clear(tbody);
    let lastInput = null;
    items.forEach((item, index) => {
      const nameInput = textControl({
        value: item.name,
        placeholder: options.namePlaceholder || '名称',
        ariaLabel: (options.nameHeader || '名称') + ' ' + (index + 1),
        on: {
          input: (event) => {
            item.name = event.target.value;
          },
        },
      });
      const valueInput = textControl({
        value: item.value,
        placeholder: options.valuePlaceholder || '值',
        ariaLabel: (options.valueHeader || '值') + ' ' + (index + 1),
        on: {
          input: (event) => {
            item.value = event.target.value;
          },
        },
      });
      lastInput = nameInput;
      const enableBox = el('input', {
        type: 'checkbox',
        checked: item.enabled,
        'aria-label': '启用第 ' + (index + 1) + ' 行',
        on: {
          change: (event) => {
            item.enabled = event.target.checked;
          },
        },
      });
      const row = el(
        'tr',
        {},
        el('td', { class: 'kv-enable' }, enableBox),
        el('td', {}, nameInput),
        el('td', {}, valueInput),
        options.withDescription
          ? el(
              'td',
              {},
              textControl({
                value: item.description,
                placeholder: '说明',
                ariaLabel: '说明 ' + (index + 1),
                on: {
                  input: (event) => {
                    item.description = event.target.value;
                  },
                },
              })
            )
          : null,
        el(
          'td',
          { class: 'kv-actions' },
          iconButton('i-trash', '删除第 ' + (index + 1) + ' 行', () => {
            items.splice(index, 1);
            renderRows();
            if (options.onChange) options.onChange(items);
          }, 'btn-sm')
        )
      );
      tbody.appendChild(row);
    });
    if (focusLast && lastInput) lastInput.focus();
  }

  const addButton = el(
    'button',
    {
      type: 'button',
      class: 'btn btn-sm',
      on: {
        click: () => {
          items.push({ name: '', value: '', enabled: true, description: '', required: false });
          renderRows(true);
        },
      },
    },
    icon('i-plus', 'icon-sm'),
    options.addLabel || '新增一行'
  );

  renderRows();
  const node = el(
    'div',
    { class: 'stack' },
    table,
    el('div', { class: 'row' }, addButton, options.hint ? el('span', { class: 'field-hint', text: options.hint }) : null)
  );

  return {
    node,
    items,
    addRow(name, value) {
      items.push({ name: name || '', value: value || '', enabled: true, description: '', required: false });
      renderRows();
    },
    /** 去掉完全空白的行，返回 KeyValue 数组。 */
    getItems() {
      return items
        .filter((item) => String(item.name).trim() !== '' || String(item.value).trim() !== '' || options.keepEmptyName)
        .map((item) => ({
          name: String(item.name).trim(),
          value: item.value,
          enabled: Boolean(item.enabled),
          description: item.description || '',
          required: Boolean(item.required),
        }));
    },
  };
}

/** 认证配置编辑器（AuthSpec：type/token/username/password/key/value/location/scheme）。 */
export function authEditor(auth, options = {}) {
  const model = {
    type: (auth && auth.type) || 'inherit',
    token: (auth && auth.token) || '',
    username: (auth && auth.username) || '',
    password: (auth && auth.password) || '',
    key: (auth && auth.key) || '',
    value: (auth && auth.value) || '',
    location: (auth && auth.location) || 'header',
    scheme: (auth && auth.scheme) || 'Bearer',
  };
  const secretMode = Boolean(options.masked);
  let tokenControl = null;
  let passwordControl = null;
  let valueControl = null;
  const fieldsHost = el('div', { class: 'grid-2' });

  function renderFields() {
    clear(fieldsHost);
    if (model.type === 'none' || model.type === 'inherit') {
      fieldsHost.appendChild(
        el('div', {
          class: 'field-hint',
          text:
            model.type === 'inherit'
              ? '继承环境中的认证配置；用例级认证留空即可。'
              : '不注入任何认证信息。',
        })
      );
      return;
    }
    if (model.type === 'bearer') {
      tokenControl = secretMode
        ? maskedControl({ id: 'auth-token', value: model.token, secret: true, ariaLabel: 'Bearer Token' })
        : null;
      const input = tokenControl ? tokenControl.node : textControl({
        id: 'auth-token',
        value: model.token,
        ariaLabel: 'Bearer Token',
        on: { input: (event) => { model.token = event.target.value; } },
      });
      fieldsHost.appendChild(formField('Token', input, { id: 'auth-token' }));
      fieldsHost.appendChild(
        formField(
          'Scheme',
          textControl({
            id: 'auth-scheme',
            value: model.scheme,
            placeholder: 'Bearer',
            ariaLabel: 'Scheme',
            on: { input: (event) => { model.scheme = event.target.value; } },
          }),
          { id: 'auth-scheme', hint: '默认 Bearer，也可填 Basic 等' }
        )
      );
      return;
    }
    if (model.type === 'basic' || model.type === 'aliyun_sls') {
      const sls = model.type === 'aliyun_sls';
      passwordControl = secretMode
        ? maskedControl({ id: 'auth-password', value: model.password, secret: true, ariaLabel: '密码' })
        : null;
      fieldsHost.appendChild(
        formField(
          sls ? 'AccessKey ID' : '用户名',
          textControl({
            id: 'auth-username',
            value: model.username,
            ariaLabel: '用户名',
            on: { input: (event) => { model.username = event.target.value; } },
          }),
          { id: 'auth-username' }
        )
      );
      fieldsHost.appendChild(
        formField(
          sls ? 'AccessKey Secret' : '密码',
          passwordControl
            ? passwordControl.node
            : textControl({
                id: 'auth-password',
                type: 'password',
                value: model.password,
                ariaLabel: '密码',
                on: { input: (event) => { model.password = event.target.value; } },
              }),
          { id: 'auth-password' }
        )
      );
      if (sls) {
        tokenControl = secretMode
          ? maskedControl({ id: 'auth-token', value: model.token, secret: true, ariaLabel: 'STS Token' })
          : null;
        fieldsHost.appendChild(formField('STS Token（可选）', tokenControl ? tokenControl.node : textControl({
          id: 'auth-token', value: model.token, type: 'password',
          on: { input: (event) => { model.token = event.target.value; } },
        }), { id: 'auth-token', hint: 'SLS 请求在发送时自动签名，建议引用环境变量。' }));
      }
      return;
    }
    if (model.type === 'api_key') {
      valueControl = secretMode
        ? maskedControl({ id: 'auth-value', value: model.value, secret: true, ariaLabel: 'Key 值' })
        : null;
      fieldsHost.appendChild(
        formField(
          'Key 名称',
          textControl({
            id: 'auth-key',
            value: model.key,
            ariaLabel: 'Key 名称',
            on: { input: (event) => { model.key = event.target.value; } },
          }),
          { id: 'auth-key', hint: '例如 X-API-Key' }
        )
      );
      fieldsHost.appendChild(
        formField(
          'Key 值',
          valueControl
            ? valueControl.node
            : textControl({
                id: 'auth-value',
                value: model.value,
                ariaLabel: 'Key 值',
                on: { input: (event) => { model.value = event.target.value; } },
              }),
          { id: 'auth-value' }
        )
      );
      fieldsHost.appendChild(
        formField(
          '位置',
          selectControl(
            [
              { value: 'header', label: '请求头 header' },
              { value: 'query', label: '查询参数 query' },
            ],
            {
              id: 'auth-location',
              value: model.location,
              on: { change: (event) => { model.location = event.target.value; } },
            }
          ),
          { id: 'auth-location' }
        )
      );
      return;
    }
    // header：自定义认证头
    valueControl = secretMode
      ? maskedControl({ id: 'auth-value', value: model.value, secret: true, ariaLabel: '取值' })
      : null;
    fieldsHost.appendChild(
      formField(
        '头名称',
        textControl({
          id: 'auth-key',
          value: model.key,
          ariaLabel: '头名称',
          on: { input: (event) => { model.key = event.target.value; } },
        }),
        { id: 'auth-key' }
      )
    );
    fieldsHost.appendChild(
      formField(
        '取值',
        valueControl
          ? valueControl.node
          : textControl({
              id: 'auth-value',
              value: model.value,
              ariaLabel: '取值',
              on: { input: (event) => { model.value = event.target.value; } },
            }),
        { id: 'auth-value' }
      )
    );
  }

  const typeSelect = enumSelect('auth_types', {
    id: 'auth-type',
    value: model.type,
    labels: {
      none: '无认证',
      inherit: '继承环境',
      bearer: 'Bearer Token',
      basic: 'Basic 用户名密码',
      api_key: 'API Key',
      header: '自定义请求头',
      aliyun_sls: '阿里云 SLS 签名',
    },
    on: {
      change: (event) => {
        // 切换类型前先保存已填内容，避免丢失
        if (tokenControl) model.token = tokenControl.read();
        if (passwordControl) model.password = passwordControl.read();
        if (valueControl) model.value = valueControl.read();
        model.type = event.target.value;
        renderFields();
      },
    },
  });

  renderFields();
  const node = el(
    'fieldset',
    { class: 'subform' },
    el('legend', { text: options.legend || '认证' }),
    el('div', { class: 'stack' }, formField('认证类型', typeSelect, { id: 'auth-type' }), fieldsHost)
  );

  return {
    node,
    getValue() {
      if (tokenControl) model.token = tokenControl.read();
      if (passwordControl) model.password = passwordControl.read();
      if (valueControl) model.value = valueControl.read();
      return {
        type: model.type,
        token: model.token,
        username: model.username,
        password: model.password,
        key: model.key,
        value: model.value,
        location: model.location,
        scheme: model.scheme,
      };
    },
  };
}

/* ==================================================== 结果展示构件 */

/** 断言结果表：期望 / 实际 / 结果 / 说明（AssertionResult）。 */
export function assertionResultsTable(assertions) {
  const list = Array.isArray(assertions) ? assertions : [];
  if (!list.length) {
    return el('div', { class: 'field-hint', text: '没有断言结果（未配置断言或未执行）' });
  }
  const cell = (value) => el('td', { class: 'mono break-all', text: formatValue(value) });
  return el(
    'div',
    { class: 'table-wrap' },
    el(
      'table',
      { class: 'data' },
      el(
        'thead',
        {},
        el(
          'tr',
          {},
          el('th', { text: '#' }),
          el('th', { text: '断言' }),
          el('th', { text: '期望' }),
          el('th', { text: '实际' }),
          el('th', { text: '结果' }),
          el('th', { text: '说明' })
        )
      ),
      el(
        'tbody',
        {},
        list.map((item) =>
          el(
            'tr',
            {},
            el('td', { text: item.index === undefined || item.index === null ? '' : String(item.index) }),
            el('td', {
              class: 'mono break-all',
              text: item.describe || item.type + (item.target ? '[' + item.target + ']' : '') + ' ' + item.operator,
            }),
            cell(item.expected),
            cell(item.actual),
            el('td', {}, statusChip(item.passed ? 'passed' : 'failed')),
            el('td', { class: 'muted', text: item.message || '' })
          )
        )
      )
    )
  );
}

function kvDictBlock(title, data, emptyText) {
  const keys = data && typeof data === 'object' ? Object.keys(data) : [];
  const wrap = el('div', { class: 'detail-item' });
  wrap.appendChild(el('dt', { text: title }));
  if (!keys.length) {
    wrap.appendChild(el('dd', { class: 'field-hint', text: emptyText || '无' }));
    return wrap;
  }
  wrap.appendChild(
    el(
      'dl',
      { class: 'kv-list' },
      keys.reduce(
        (acc, key) => acc.concat([el('dt', { text: key }), el('dd', { text: formatValue(data[key]) })]),
        []
      )
    )
  );
  return wrap;
}

/** 请求预览：兼容 PreparedRequest.preview 与任意字典。 */
export function requestPreviewBlock(request) {
  const data = request && typeof request === 'object' ? request : {};
  const keys = Object.keys(data);
  if (!keys.length) return el('div', { class: 'field-hint', text: '无请求预览（用例未执行或请求未构建）' });
  const nodes = [];
  const head = [];
  if (data.method) head.push(el('span', { class: 'method method-' + String(data.method).toUpperCase(), text: data.method }));
  if (data.url) head.push(el('span', { class: 'mono break-all', text: data.url }));
  if (data.body_type) head.push(el('span', { class: 'muted', text: 'body_type=' + data.body_type }));
  if (head.length) nodes.push(el('div', { class: 'row' }, head));
  const headers = data.headers;
  if (headers && typeof headers === 'object') nodes.push(kvDictBlock('请求头', headers));
  if (data.body !== null && data.body !== undefined && data.body !== '') {
    nodes.push(el('div', { class: 'detail-item' }, el('dt', { text: '请求体' }), el('pre', { text: truncate(formatJson(data.body), 4000) })));
  }
  const handled = ['method', 'url', 'headers', 'body', 'body_type'];
  const rest = {};
  keys.forEach((key) => {
    if (handled.indexOf(key) === -1) rest[key] = data[key];
  });
  if (Object.keys(rest).length) {
    nodes.push(el('details', {}, el('summary', { text: '其他字段' }), el('pre', { text: formatJson(rest) })));
  }
  return el('div', { class: 'stack' }, nodes);
}

/** 响应预览：HttpResponse 结构。 */
export function responseBlock(response) {
  const data = response && typeof response === 'object' ? response : {};
  if (!Object.keys(data).length) return el('div', { class: 'field-hint', text: '无响应（请求未完成）' });
  const nodes = [];
  const head = [];
  head.push(statusChip(data.ok ? 'passed' : 'error'));
  head.push(el('span', { class: 'mono', text: 'HTTP ' + (data.status === null || data.status === undefined ? '—' : data.status) }));
  head.push(el('span', { class: 'muted', text: formatDuration(data.duration_ms) }));
  if (data.size_bytes) head.push(el('span', { class: 'muted', text: formatBytes(data.size_bytes) }));
  if (data.truncated) head.push(el('span', { class: 'chip chip-incomplete', text: '响应体已截断' }));
  nodes.push(el('div', { class: 'row' }, head));
  if (data.error) nodes.push(el('div', { class: 'banner banner-error' }, icon('i-alert'), el('span', { text: data.error })));
  if (data.headers && Object.keys(data.headers).length) nodes.push(kvDictBlock('响应头', data.headers));
  const bodyText =
    data.body_json !== null && data.body_json !== undefined
      ? formatJson(data.body_json)
      : String(data.body_text === undefined || data.body_text === null ? '' : data.body_text);
  nodes.push(
    el(
      'div',
      { class: 'detail-item' },
      el('dt', { text: '响应体' }),
      bodyText ? el('pre', { text: truncate(bodyText, 6000) }) : el('div', { class: 'field-hint', text: '（空响应体）' })
    )
  );
  return el('div', { class: 'stack' }, nodes);
}

/**
 * 单条 CaseResult 的完整展示（用例运行面板与运行详情共用）。
 * @param {object} result CaseResult
 * @param {{showName?:boolean, open?:boolean}} [options]
 */
export function caseResultBlock(result, options = {}) {
  const data = result && typeof result === 'object' ? result : {};
  const nodes = [];
  const head = [statusChip(data.status)];
  if (options.showName !== false && data.case_name) {
    head.push(el('strong', { text: data.case_name }));
  }
  if (data.case_id !== null && data.case_id !== undefined) {
    head.push(el('span', { class: 'faint mono', text: '#' + data.case_id }));
  }
  head.push(el('span', { class: 'muted', text: '耗时 ' + formatDuration(data.duration_ms) }));
  if (data.attempts) head.push(el('span', { class: 'muted', text: '尝试 ' + data.attempts + ' 次' }));
  if (data.started_at) head.push(el('span', { class: 'faint', text: formatDateTime(data.started_at) }));
  nodes.push(el('div', { class: 'row' }, head));

  if (data.error) {
    nodes.push(el('div', { class: 'banner banner-error' }, icon('i-alert'), el('span', { text: data.error })));
  }

  nodes.push(
    el(
      'div',
      { class: 'grid-2' },
      el('div', { class: 'detail-item' }, el('dt', { text: '请求预览' }), el('dd', {}, requestPreviewBlock(data.request))),
      el('div', { class: 'detail-item' }, el('dt', { text: '响应' }), el('dd', {}, responseBlock(data.response)))
    )
  );

  nodes.push(el('h4', { text: '断言结果' }));
  nodes.push(assertionResultsTable(data.assertions));

  const extracted = data.extracted && typeof data.extracted === 'object' ? Object.keys(data.extracted) : [];
  if (extracted.length) {
    nodes.push(el('h4', { text: '提取变量' }));
    nodes.push(kvDictBlock('提取变量', data.extracted));
  }

  if (Array.isArray(data.steps) && data.steps.length) {
    nodes.push(el('h4', { text: '前置/后置步骤' }));
    nodes.push(el('pre', { text: truncate(formatJson(data.steps), 4000) }));
  }
  if (Array.isArray(data.logs) && data.logs.length) {
    nodes.push(el('h4', { text: '日志' }));
    nodes.push(el('pre', { text: truncate(data.logs.join('\n'), 4000) }));
  }
  return el('div', { class: 'stack' }, nodes);
}

/* ============================================================ 路由 */

const NAV_ITEMS = [
  { key: 'dashboard', label: '概览', icon: 'i-dashboard' },
  { key: 'endpoints', label: '接口管理', icon: 'i-endpoint' },
  { key: 'cases', label: '用例管理', icon: 'i-case' },
  { key: 'suites', label: '套件', icon: 'i-suite' },
  { key: 'environments', label: '环境', icon: 'i-env' },
  { key: 'runs', label: '运行与历史', icon: 'i-run' },
  { key: 'imports', label: '导入', icon: 'i-import' },
];

const VIEW_LOADERS = {
  dashboard: () => import('./views/dashboard.js'),
  endpoints: () => import('./views/endpoints.js'),
  cases: () => import('./views/cases.js'),
  suites: () => import('./views/suites.js'),
  environments: () => import('./views/environments.js'),
  runs: () => import('./views/runs.js'),
  run_detail: () => import('./views/run_detail.js'),
  imports: () => import('./views/imports.js'),
};

const viewCache = new Map();
let currentAbort = null;

function decodeSegment(text) {
  try {
    return decodeURIComponent(text);
  } catch (err) {
    return text;
  }
}

export function parseRoute(hash) {
  const clean = String(hash || '').replace(/^#/, '');
  const parts = clean.split('/').filter((part) => part !== '').map(decodeSegment);
  if (!parts.length || parts[0] === 'projects') return { name: 'projects', pid: '', arg: '', parts };
  if (parts[0] === 'p' && parts[1]) {
    const pid = parts[1];
    const view = parts[2] || 'dashboard';
    if (view === 'runs' && parts[3]) return { name: 'run_detail', pid, arg: parts[3], parts };
    if (view === 'cases' && parts[3]) return { name: 'cases', pid, arg: parts[3], parts };
    if (VIEW_LOADERS[view]) return { name: view, pid, arg: '', parts };
    return { name: 'not_found', pid, arg: view, parts };
  }
  return { name: 'not_found', pid: '', arg: parts.join('/'), parts };
}

export function routeHash(pid, view, arg) {
  const suffix = arg === undefined || arg === null || arg === '' ? '' : '/' + encodeURIComponent(arg);
  return '#/p/' + encodeURIComponent(pid) + '/' + view + suffix;
}

export function navigate(hash) {
  if (window.location.hash === hash) {
    handleRoute();
    return;
  }
  window.location.hash = hash;
}

async function loadViewModule(name) {
  if (!viewCache.has(name)) viewCache.set(name, VIEW_LOADERS[name]());
  return viewCache.get(name);
}

function renderProjectSwitcher(route) {
  const host = document.getElementById('project-switcher');
  clear(host);
  const options = [{ value: '', label: state.projects.length ? '选择项目…' : '暂无项目' }].concat(
    state.projects.map((project) => ({ value: project.id, label: project.name + '（' + project.id + '）' }))
  );
  const select = el('select', {
    id: 'project-select',
    'aria-label': '切换项目',
    disabled: !state.projects.length,
    on: {
      change: (event) => {
        const pid = event.target.value;
        if (pid) navigate(routeHash(pid, 'dashboard'));
      },
    },
  });
  options.forEach((option) => {
    select.appendChild(
      el('option', { value: option.value, text: option.label, selected: option.value === (route.pid || '') })
    );
  });
  host.appendChild(el('label', { class: 'field-label', for: 'project-select', text: '项目' }));
  host.appendChild(select);
  if (route.pid && !state.projects.some((project) => project.id === route.pid)) {
    host.appendChild(el('span', { class: 'chip chip-incomplete', text: '路由项目未在列表中' }));
  }
}

/**
 * 顶栏环境切换器：切到哪个环境，后续的试跑 / 运行 / 变量解析就都用它。
 *
 * 语义上"当前环境"就是该项目的**默认环境**（服务端 is_default）——
 * 不额外引入"界面专用环境"这类隐藏状态，CLI 与 CI 不带 --env 时行为完全一致。
 */
function renderEnvSwitcher(route) {
  const host = document.getElementById('env-switcher');
  if (!host) return;
  clear(host);
  if (!route.pid) return;

  const environments = state.environmentsPid === route.pid ? state.environments : [];
  const active = environments.find((item) => item.is_default) || null;

  const select = el('select', {
    id: 'env-select',
    'aria-label': '切换环境',
    disabled: !environments.length,
    on: {
      change: async (event) => {
        const target = event.target.value;
        if (!target) return;
        const environment = environments.find((item) => String(item.id) === target);
        select.disabled = true;
        try {
          await Api.setDefaultEnvironment(route.pid, Number(target));
          state.environmentsPid = null; // 失效缓存，下一次渲染重新拉取
          await refreshEnvironments(route.pid);
          toast('已切换到环境「' + ((environment && environment.name) || target) + '」', 'success');
          handleRoute(); // 当前视图按新环境重新加载
        } catch (err) {
          toast('切换环境失败：' + ((err && err.message) || '未知错误'), 'error');
          select.disabled = false;
        }
      },
    },
  });

  if (!environments.length) {
    select.appendChild(el('option', { value: '', text: '未配置环境' }));
  } else {
    environments.forEach((environment) => {
      const label = environment.name + (environment.base_url ? ' · ' + environment.base_url : ' · 未配置地址');
      select.appendChild(
        el('option', {
          value: String(environment.id),
          text: label,
          selected: Boolean(environment.is_default),
        })
      );
    });
  }

  // 显式对齐服务端的默认环境，避免"选项没标 selected 时浏览器默认选第一个"造成误判
  if (active) select.value = String(active.id);

  host.appendChild(el('label', { class: 'field-label', for: 'env-select', text: '环境' }));
  host.appendChild(select);
  host.appendChild(
    el(
      'a',
      {
        class: 'topbar-env-manage',
        href: routeHash(route.pid, 'environments'),
        title: '管理环境与变量',
        'aria-label': '管理环境与变量',
      },
      icon('i-env')
    )
  );
  if (!active) {
    host.appendChild(el('span', { class: 'chip chip-incomplete', text: '未设默认环境' }));
  }
}

async function refreshEnvironments(pid) {
  if (!pid) {
    state.environments = [];
    state.environmentsPid = null;
    return [];
  }
  try {
    const data = await Api.listEnvironments(pid);
    state.environments = itemsOf(data);
    state.environmentsPid = pid;
  } catch (err) {
    state.environments = [];
    state.environmentsPid = null;
  }
  return state.environments;
}

/** 可选主题：午夜（深色）/ 暖砂（暖色浅底）/ 极光（浅色渐变玻璃）/ 白昼（中性浅色）。 */
export const THEMES = [
  { id: 'aurora', label: '极光', hint: '浅色渐变 + 玻璃质感（默认）' },
  { id: 'midnight', label: '午夜', hint: '深色，长时间看不累' },
  { id: 'sand', label: '暖砂', hint: '暖色纸张感' },
  { id: 'daylight', label: '白昼', hint: '中性浅色（原样式）' },
];

const THEME_KEY = 'hapitest.theme';

export const DEFAULT_THEME = 'aurora';

export function currentTheme() {
  // 以 DOM 上的实际主题为准（首屏脚本可能来自 ?theme= 或 localStorage），
  // 否则下拉框会和页面对不上
  const applied = document.documentElement.getAttribute('data-theme');
  if (applied) return applied;
  try {
    return localStorage.getItem(THEME_KEY) || DEFAULT_THEME;
  } catch (err) {
    return DEFAULT_THEME;
  }
}

export function applyTheme(themeId) {
  const known = THEMES.some((item) => item.id === themeId);
  const value = known ? themeId : DEFAULT_THEME;
  document.documentElement.setAttribute('data-theme', value);
  try {
    localStorage.setItem(THEME_KEY, value);
  } catch (err) { /* 忽略写入失败 */ }
  return value;
}

function renderThemeSwitcher() {
  const host = document.getElementById('theme-switcher');
  if (!host) return;
  clear(host);
  host.appendChild(el('label', { class: 'field-label', for: 'theme-select', text: '主题' }));
  const select = el('select', {
    id: 'theme-select',
    'aria-label': '切换主题',
    title: '切换界面主题（保存在本机浏览器）',
    on: {
      change: (event) => {
        const value = applyTheme(event.target.value);
        const theme = THEMES.find((item) => item.id === value);
        toast('已切换到「' + (theme ? theme.label : value) + '」主题', 'success');
      },
    },
  });
  const active = currentTheme();
  // 首次访问（既没有 data-theme 也没有本地记录）时把默认主题真正应用上，
  // 避免"下拉显示午夜、页面还是浅色"的错位
  if (!document.documentElement.getAttribute('data-theme')) applyTheme(active);
  THEMES.forEach((theme) => {
    select.appendChild(
      el('option', { value: theme.id, text: theme.label, title: theme.hint, selected: theme.id === active })
    );
  });
  if (active) select.value = active;
  host.appendChild(select);
}

function renderSidebar(route) {
  const host = document.getElementById('sidebar');
  clear(host);

  if (route.pid) {
    const project = state.projects.find((item) => item.id === route.pid);
    host.appendChild(
      el(
        'div',
        { class: 'sidebar-project' },
        el('strong', { text: project ? project.name : route.pid }),
        // 名称与 ID 相同时不重复占一行
        project && project.name !== route.pid ? el('span', { class: 'mono', text: route.pid }) : null
      )
    );
    const list = el('div');
    NAV_ITEMS.forEach((item) => {
      const active = route.name === item.key || (item.key === 'runs' && route.name === 'run_detail');
      list.appendChild(
        el(
          'a',
          {
            class: 'nav-link',
            href: routeHash(route.pid, item.key),
            'aria-current': active ? 'page' : null,
          },
          icon(item.icon),
          el('span', { text: item.label })
        )
      );
    });
    host.appendChild(list);
  }

  host.appendChild(el('div', { class: 'sidebar-group-title', text: '工作区' }));
  host.appendChild(
    el(
      'a',
      { class: 'nav-link', href: '#/projects', 'aria-current': route.name === 'projects' ? 'page' : null },
      icon('i-suite'),
      el('span', { text: '项目列表' })
    )
  );
}

function renderFooter(route) {
  const versionNode = document.getElementById('footer-version');
  const projectNode = document.getElementById('footer-project');
  const health = state.health;
  if (health) {
    versionNode.textContent =
      (health.name || 'HapiTest') + ' v' + (health.version || '?') + ' · 协议 ' + (health.protocol || '?');
  } else {
    versionNode.textContent = 'HapiTest（版本信息不可用）';
  }
  projectNode.textContent = route.pid ? '当前项目：' + route.pid : '未选择项目';
}

async function refreshProjects() {
  try {
    state.projects = itemsOf(await Api.listProjects());
  } catch (err) {
    state.projects = [];
  }
  return state.projects;
}

async function handleRoute() {
  const route = parseRoute(window.location.hash);
  const main = document.getElementById('main');

  if (!window.location.hash || window.location.hash === '#' || window.location.hash === '#/') {
    const last = readLastProjectId();
    if (last) {
      window.location.replace(routeHash(last, 'dashboard'));
      return;
    }
    window.location.replace('#/projects');
    return;
  }

  if (currentAbort) currentAbort.abort();
  currentAbort = new AbortController();
  closeAllOverlays();
  setLoading(true);

  if (!state.projects.length) await refreshProjects();
  if (!state.health) await loadHealth();
  if (!state.meta) await loadMeta();

  if (route.pid) rememberProjectId(route.pid);
  state.currentProject = state.projects.find((item) => item.id === route.pid) || null;

  if (route.pid && state.environmentsPid !== route.pid) {
    await refreshEnvironments(route.pid);
  }
  renderProjectSwitcher(route);
  renderEnvSwitcher(route);
  renderThemeSwitcher();
  renderSidebar(route);
  renderFooter(route);

  clear(main);
  // 声明式定位：视图可写入标题
  const ctx = {
    pid: route.pid,
    arg: route.arg,
    route,
    container: main,
    signal: currentAbort.signal,
    setTitle: (text) => {
      document.title = text + ' · HapiTest';
    },
    reload: () => handleRoute(),
    navigate,
  };

  const navItem = NAV_ITEMS.find((item) => item.key === route.name);
  ctx.setTitle(route.name === 'projects' ? '项目选择' : navItem ? navItem.label : '未知页面');

  try {
    if (route.name === 'projects') {
      await renderProjects(ctx);
    } else if (route.name === 'not_found') {
      renderNotFound(ctx);
    } else {
      const module = await loadViewModule(route.name);
      await module.render(ctx);
    }
  } catch (err) {
    if (err && err.name === 'AbortError') return;
    clear(main);
    main.appendChild(errorBanner(err));
    main.appendChild(
      el('p', { class: 'muted', text: '页面渲染失败：' + ((err && err.message) || '未知错误') })
    );
  } finally {
    if (!currentAbort.signal.aborted) setLoading(false);
  }

  window.scrollTo({ top: 0, behavior: 'auto' });
  if (main.focus) main.focus({ preventScroll: true });
}

function renderNotFound(ctx) {
  clear(ctx.container);
  ctx.container.appendChild(
    el(
      'div',
      { class: 'card' },
      el(
        'div',
        { class: 'card-body' },
        el('h1', { text: '页面不存在' }),
        el('p', { class: 'muted', text: '无法识别的路由：' + (ctx.route.parts.join('/') || '(空)') }),
        el('a', { class: 'btn btn-primary', href: '#/projects', text: '返回项目列表' })
      )
    )
  );
}

/* ======================================================= 项目选择视图 */

const COUNT_FIELDS = [
  { key: 'endpoint', label: '接口' },
  { key: 'test_case', label: '用例' },
  { key: 'suite', label: '套件' },
  { key: 'environment', label: '环境' },
  { key: 'run', label: '运行' },
];

function projectCount(project, key) {
  const counts = (project && project.counts) || {};
  const value = counts[key];
  return typeof value === 'number' ? value : 0;
}

function projectFormFields(project) {
  const isEdit = Boolean(project);
  const nameInput = el('input', {
    type: 'text',
    id: 'project-name',
    value: (project && project.name) || '',
    required: true,
    maxlength: 120,
    autocomplete: 'off',
  });
  const idInput = el('input', {
    type: 'text',
    id: 'project-id',
    value: (project && project.id) || '',
    placeholder: 'hapi-air',
    autocomplete: 'off',
    disabled: isEdit,
    spellcheck: 'false',
  });
  const descriptionInput = el('textarea', { id: 'project-description', rows: 2 });
  descriptionInput.value = (project && project.description) || '';
  const baseUrlInput = el('input', {
    type: 'text',
    id: 'project-base-url',
    value: (project && project.base_url) || '',
    placeholder: 'http://127.0.0.1:8080',
    autocomplete: 'off',
    spellcheck: 'false',
  });

  const form = el(
    'form',
    { class: 'stack', novalidate: true },
    el(
      'div',
      { class: 'grid-2' },
      el('div', { class: 'field' }, el('label', { for: 'project-name' }, '项目名称 *'), nameInput),
      el(
        'div',
        { class: 'field' },
        el('label', { for: 'project-id' }, '项目 ID'),
        idInput,
        el('span', { class: 'field-hint', text: isEdit ? '项目 ID 不可修改' : '留空则由名称自动生成' })
      )
    ),
    el('div', { class: 'field' }, el('label', { for: 'project-base-url' }, '默认 base_url'), baseUrlInput),
    el('div', { class: 'field' }, el('label', { for: 'project-description' }, '描述'), descriptionInput)
  );

  const read = () => {
    const payload = {
      name: nameInput.value.trim(),
      description: descriptionInput.value.trim(),
      base_url: baseUrlInput.value.trim(),
    };
    if (!isEdit && idInput.value.trim()) payload.id = idInput.value.trim();
    return payload;
  };

  return { form, read, nameInput };
}

async function renderProjects(ctx) {
  await refreshProjects();
  const main = ctx.container;
  clear(main);

  const pageHead = el(
    'div',
    { class: 'page-head' },
    el('h1', { text: '项目选择' }),
    el('span', { class: 'muted', text: '共 ' + state.projects.length + ' 个项目' }),
    el('div', { class: 'page-sub', text: '每个项目拥有独立的接口目录、用例、套件、环境与运行历史。' })
  );
  main.appendChild(pageHead);

  const { form, read } = projectFormFields(null);
  const submitButton = el('button', { type: 'submit', class: 'btn btn-primary' }, icon('i-plus'), '创建项目');
  form.appendChild(el('div', { class: 'row' }, submitButton));

  const createCard = el(
    'section',
    { class: 'card section' },
    el('div', { class: 'card-head' }, el('h2', { text: '新建项目' })),
    el('div', { class: 'card-body' }, form)
  );
  main.appendChild(createCard);

  const tableWrap = el('div', { class: 'table-wrap' });
  const listCard = el(
    'section',
    { class: 'card section' },
    el('div', { class: 'card-head' }, el('h2', { text: '项目列表' })),
    tableWrap
  );
  main.appendChild(listCard);

  function renderTable() {
    clear(tableWrap);
    if (!state.projects.length) {
      tableWrap.appendChild(emptyBlock('还没有项目', '使用上方表单创建第一个项目。'));
      return;
    }
    const rows = state.projects.map((project) => {
      const counts = COUNT_FIELDS.map((field) =>
        el(
          'td',
          { class: 'nowrap' },
          el('span', { class: 'pill-count', text: String(projectCount(project, field.key)), title: field.label })
        )
      );
      return el(
        'tr',
        {},
        el(
          'td',
          {},
          el('a', { href: routeHash(project.id, 'dashboard'), text: project.name || project.id }),
          el('div', { class: 'mono faint', text: project.id })
        ),
        el('td', { class: 'muted', text: project.description || '—' }),
        el('td', { class: 'mono break-all', text: project.base_url || '—' }),
        counts,
        el(
          'td',
          { class: 'actions' },
          el('a', { class: 'btn btn-sm btn-primary', href: routeHash(project.id, 'dashboard') }, '打开'),
          ' ',
          el('button', {
            type: 'button',
            class: 'btn btn-sm',
            text: '编辑',
            on: { click: () => openEditProject(project) },
          }),
          ' ',
          el('button', {
            type: 'button',
            class: 'btn btn-sm btn-danger',
            text: '删除',
            on: { click: () => removeProject(project) },
          })
        )
      );
    });

    const table = el(
      'table',
      { class: 'data' },
      el(
        'thead',
        {},
        el(
          'tr',
          {},
          el('th', { text: '项目' }),
          el('th', { text: '描述' }),
          el('th', { text: 'base_url' }),
          COUNT_FIELDS.map((field) => el('th', { class: 'nowrap', text: field.label })),
          el('th', { class: 'nowrap', text: '操作' })
        )
      ),
      el('tbody', {}, rows)
    );
    tableWrap.appendChild(table);
  }

  function openEditProject(project) {
    const editor = projectFormFields(project);
    const handle = openModal({
      title: '编辑项目 ' + project.id,
      content: editor.form,
      footer: [
        el('div', { class: 'spacer' }),
        el('button', { type: 'button', class: 'btn', text: '取消', on: { click: () => handle.close() } }),
        el('button', {
          type: 'button',
          class: 'btn btn-primary',
          text: '保存',
          on: {
            click: async () => {
              const payload = editor.read();
              if (!payload.name) {
                toast('项目名称不能为空', 'error');
                editor.nameInput.focus();
                return;
              }
              delete payload.id;
              const [ok] = await runAction(() => Api.updateProject(project.id, payload), '项目已更新');
              if (ok) {
                handle.close();
                await refreshProjects();
                renderTable();
                renderProjectSwitcher(ctx.route);
                renderSidebar(ctx.route);
              }
            },
          },
        }),
      ],
    });
  }

  async function removeProject(project) {
    const confirmed = await confirmDialog({
      title: '删除项目',
      message: '将删除项目「' + (project.name || project.id) + '」及其全部数据目录、接口、用例与运行历史，且不可恢复。',
      requireText: project.id,
      confirmLabel: '永久删除',
    });
    if (!confirmed) return;
    const [ok] = await runAction(() => Api.deleteProject(project.id), '项目已删除');
    if (ok) {
      if (readLastProjectId() === project.id) rememberProjectId('');
      await refreshProjects();
      renderTable();
      renderProjectSwitcher(ctx.route);
    }
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const payload = read();
    if (!payload.name) {
      toast('项目名称不能为空', 'error');
      return;
    }
    submitButton.disabled = true;
    const [ok] = await runAction(() => Api.createProject(payload), '项目「' + payload.name + '」已创建');
    submitButton.disabled = false;
    if (ok) {
      form.reset();
      await refreshProjects();
      renderTable();
      renderProjectSwitcher(ctx.route);
    }
  });

  renderTable();
}

/* ============================================================ 启动 */

function boot() {
  const skip = document.getElementById('skip-link');
  if (skip) {
    skip.addEventListener('click', () => {
      const main = document.getElementById('main');
      if (main) main.focus();
    });
  }
  const refreshButton = document.getElementById('refresh-button');
  if (refreshButton) {
    refreshButton.addEventListener('click', () => handleRoute());
  }
  window.addEventListener('hashchange', () => {
    handleRoute();
  });
  handleRoute();
}

boot();
