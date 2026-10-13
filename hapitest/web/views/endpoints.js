/**
 * 接口管理：GET/POST /api/v1/projects/{pid}/endpoints
 * 详情 / 编辑（PUT）/ 删除（DELETE）/ 生成用例（to-case）/ 试跑（probe）。
 */

import { Api } from '../api.js';
import {
  authEditor,
  checkboxControl,
  clear,
  confirmDialog,
  el,
  emptyBlock,
  enumOf,
  errorBanner,
  formField,
  formatDateTime,
  formatDuration,
  formatJson,
  html,
  icon,
  itemsOf,
  kvEditor,
  loadingBlock,
  mount,
  numberControl,
  openDrawer,
  openModal,
  runAction,
  selectControl,
  statusChip,
  textControl,
  textareaControl,
  toast,
  totalOf,
} from '../app.js';

const PAGE_SIZE = 50;

function methodChip(method) {
  return html`<span class="method method-${method}">${method}</span>`;
}

function requestPreviewBlock(request) {
  const data = request || {};
  const rows = [
    ['method', data.method],
    ['path', data.path],
    ['base_url', data.base_url],
    ['body_type', data.body_type],
    ['timeout_ms', data.timeout_ms],
    ['follow_redirects', data.follow_redirects],
    ['verify_tls', data.verify_tls],
  ];
  return el(
    'dl',
    { class: 'kv-list' },
    rows
      .filter((row) => row[1] !== null && row[1] !== undefined && row[1] !== '')
      .map((row) => [el('dt', { text: row[0] }), el('dd', { text: String(row[1]) })])
      .reduce((acc, pair) => acc.concat(pair), [])
  );
}

function kvTable(title, items) {
  const list = Array.isArray(items) ? items : [];
  if (!list.length) {
    return el('div', { class: 'field-hint', text: title + '：无' });
  }
  const table = el(
    'table',
    { class: 'data' },
    el(
      'thead',
      {},
      el('tr', {}, el('th', { text: title }), el('th', { text: '值' }), el('th', { text: '启用' }), el('th', { text: '说明' }))
    ),
    el(
      'tbody',
      {},
      list.map((item) =>
        el(
          'tr',
          {},
          el('td', { class: 'mono', text: item.name || '' }),
          el('td', { class: 'mono break-all', text: item.value === undefined || item.value === null ? '' : String(item.value) }),
          el('td', { text: item.enabled === false ? '否' : '是' }),
          el('td', { class: 'muted', text: item.description || '' })
        )
      )
    )
  );
  return el('div', { class: 'table-wrap' }, table);
}

function paramsTable(params) {
  const list = Array.isArray(params) ? params : [];
  if (!list.length) return el('div', { class: 'field-hint', text: '参数元数据：无' });
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
          el('th', { text: '名称' }),
          el('th', { text: '位置' }),
          el('th', { text: '类型' }),
          el('th', { text: '必填' }),
          el('th', { text: '示例' }),
          el('th', { text: '说明' })
        )
      ),
      el(
        'tbody',
        {},
        list.map((param) =>
          el(
            'tr',
            {},
            el('td', { class: 'mono', text: param.name || '' }),
            el('td', { text: param.location || '' }),
            el('td', { class: 'mono', text: param.type || '' }),
            el('td', { text: param.required ? '是' : '否' }),
            el('td', {
              class: 'mono break-all',
              text: param.example === null || param.example === undefined ? '—' : String(param.example),
            }),
            el('td', {
              class: 'muted',
              text:
                (param.description || '') +
                (Array.isArray(param.enum_values) && param.enum_values.length
                  ? '（枚举：' + param.enum_values.join(' / ') + '）'
                  : ''),
            })
          )
        )
      )
    )
  );
}

/**
 * 接口编辑表单（新建/编辑共用）。
 * @returns {{node:Node, read:Function, body:object}}
 */
export function endpointForm(spec, options = {}) {
  const model = {
    name: (spec && spec.name) || '',
    folder: (spec && spec.folder) || '',
    method: (spec && spec.method) || 'GET',
    path: (spec && spec.path) || '/',
    summary: (spec && spec.summary) || '',
    description: (spec && spec.description) || '',
    tags: ((spec && spec.tags) || []).join(', '),
    deprecated: Boolean(spec && spec.deprecated),
    params: (spec && spec.params) || [],
    source: (spec && spec.source) || 'manual',
    source_ref: (spec && spec.source_ref) || '',
  };
  const request = (spec && spec.request) || {};

  const nameInput = textControl({
    id: 'endpoint-name',
    value: model.name,
    on: { input: (event) => { model.name = event.target.value; } },
  });
  const folderInput = textControl({
    id: 'endpoint-folder',
    value: model.folder,
    placeholder: '例如 用户中心',
    on: { input: (event) => { model.folder = event.target.value; } },
  });
  const methodSelect = selectControl(
    enumOf('http_methods').map((value) => ({ value, label: value })),
    {
      id: 'endpoint-method',
      value: model.method,
      on: { change: (event) => { model.method = event.target.value; } },
    }
  );
  const pathInput = textControl({
    id: 'endpoint-path',
    value: model.path,
    placeholder: '/api/v1/users/{id}',
    on: { input: (event) => { model.path = event.target.value; } },
  });
  const summaryInput = textControl({
    id: 'endpoint-summary',
    value: model.summary,
    on: { input: (event) => { model.summary = event.target.value; } },
  });
  const tagsInput = textControl({
    id: 'endpoint-tags',
    value: model.tags,
    placeholder: '多个标签用英文逗号分隔',
    on: { input: (event) => { model.tags = event.target.value; } },
  });
  const descriptionArea = textareaControl({
    id: 'endpoint-description',
    rows: 3,
    value: model.description,
    on: { input: (event) => { model.description = event.target.value; } },
  });
  const deprecatedBox = checkboxControl({
    id: 'endpoint-deprecated',
    label: '已废弃（deprecated）',
    checked: model.deprecated,
    on: { change: (event) => { model.deprecated = event.target.checked; } },
  });

  const baseUrlInput = textControl({
    id: 'endpoint-base-url',
    value: request.base_url || '',
    placeholder: '留空则使用环境/项目 base_url',
    on: { input: (event) => { request.base_url = event.target.value; } },
  });
  const timeoutInput = numberControl({
    id: 'endpoint-timeout',
    value: request.timeout_ms === null || request.timeout_ms === undefined ? '' : request.timeout_ms,
    min: 1,
    on: { input: (event) => { request.timeout_ms = event.target.value; } },
  });
  const followBox = checkboxControl({
    id: 'endpoint-follow',
    label: '跟随重定向',
    checked: request.follow_redirects !== false,
    on: { change: (event) => { request.follow_redirects = event.target.checked; } },
  });
  const tlsBox = checkboxControl({
    id: 'endpoint-verify-tls',
    label: '校验 TLS 证书',
    checked: request.verify_tls !== false,
    on: { change: (event) => { request.verify_tls = event.target.checked; } },
  });

  const queryEditor = kvEditor({
    items: request.query,
    nameHeader: 'Query 名',
    valueHeader: 'Query 值',
    addLabel: '新增查询参数',
    hint: '仅启用的参数会出现在请求上；值为空也会保留 ?k=',
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

  const bodyTypeSelect = selectControl(
    enumOf('body_types').map((value) => ({ value, label: value })),
    {
      id: 'endpoint-body-type',
      value: request.body_type || 'none',
      on: {
        change: (event) => {
          request.body_type = event.target.value;
          bodyArea.disabled = request.body_type === 'none';
          validateBody(true);
        },
      },
    }
  );
  const bodyArea = textareaControl({
    id: 'endpoint-body',
    rows: 6,
    value:
      request.body === null || request.body === undefined
        ? ''
        : typeof request.body === 'string'
          ? request.body
          : formatJson(request.body),
    on: {
      input: (event) => {
        request.body = event.target.value;
        validateBody(false);
      },
      blur: () => validateBody(true),
    },
  });
  bodyArea.disabled = (request.body_type || 'none') === 'none';
  const bodyHint = el('span', { class: 'field-hint' });

  function validateBody(showOk) {
    const type = request.body_type || 'none';
    const text = bodyArea.value;
    if (type !== 'json' || !text.trim()) {
      bodyArea.removeAttribute('aria-invalid');
      bodyHint.className = 'field-hint';
      bodyHint.textContent = type === 'json' ? '请输入合法 JSON 对象，例如 {"name": "hapi"}' : 'body_type 为 none 时不发送请求体';
      return true;
    }
    try {
      JSON.parse(text);
      bodyArea.removeAttribute('aria-invalid');
      bodyHint.className = 'field-hint';
      bodyHint.textContent = showOk ? 'JSON 校验通过' : 'JSON 格式合法';
      return true;
    } catch (err) {
      bodyArea.setAttribute('aria-invalid', 'true');
      bodyHint.className = 'field-error';
      bodyHint.textContent = 'JSON 解析失败：' + err.message;
      return false;
    }
  }
  validateBody(false);

  const authBlock = authEditor(request.auth || {}, { legend: '认证（接口模板）' });

  const node = el(
    'form',
    { class: 'stack', novalidate: true },
    el(
      'div',
      { class: 'grid-2' },
      formField('接口名称 *', nameInput, { id: 'endpoint-name' }),
      formField('目录 folder', folderInput, { id: 'endpoint-folder' })
    ),
    el(
      'div',
      { class: 'grid-2' },
      formField('方法 method', methodSelect, { id: 'endpoint-method' }),
      formField('路径 path *', pathInput, { id: 'endpoint-path', hint: '路径参数写作 {id}' })
    ),
    formField('摘要 summary', summaryInput, { id: 'endpoint-summary' }),
    formField('标签 tags', tagsInput, { id: 'endpoint-tags' }),
    formField('描述 description', descriptionArea, { id: 'endpoint-description' }),
    deprecatedBox.node,
    el(
      'fieldset',
      { class: 'subform' },
      el('legend', { text: '请求模板' }),
      el(
        'div',
        { class: 'stack' },
        el(
          'div',
          { class: 'grid-2' },
          formField('base_url 覆盖', baseUrlInput, { id: 'endpoint-base-url' }),
          formField('超时 timeout_ms', timeoutInput, { id: 'endpoint-timeout' })
        ),
        el('div', { class: 'row' }, followBox.node, tlsBox.node),
        el(
          'div',
          { class: 'subform-title' },
          '查询参数 query'
        ),
        queryEditor.node,
        el('div', { class: 'subform-title' }, '请求头 headers'),
        headerEditor.node,
        el('div', { class: 'subform-title' }, 'Cookie'),
        cookieEditor.node,
        el(
          'div',
          { class: 'grid-2' },
          formField('请求体类型 body_type', bodyTypeSelect, { id: 'endpoint-body-type' }),
          formField('请求体 body', bodyArea, { id: 'endpoint-body' })
        ),
        bodyHint,
        authBlock.node
      )
    ),
    el('div', {
      class: 'field-hint',
      text: '参数元数据（params）由导入器生成，编辑请求模板不会清除它们。',
    })
  );

  function read() {
    const bodyType = request.body_type || 'none';
    let body = null;
    if (bodyType === 'json') {
      const text = bodyArea.value.trim();
      if (text) {
        try {
          body = JSON.parse(text);
        } catch (err) {
          bodyArea.focus();
          bodyArea.setAttribute('aria-invalid', 'true');
          throw new Error('请求体不是合法 JSON：' + err.message);
        }
      }
    } else if (bodyType !== 'none') {
      body = bodyArea.value;
    }
    const timeoutRaw = timeoutInput.value.trim();
    return {
      name: nameInput.value.trim(),
      folder: folderInput.value.trim(),
      method: methodSelect.value,
      path: pathInput.value.trim() || '/',
      summary: summaryInput.value.trim(),
      description: descriptionArea.value,
      tags: tagsInput.value
        .split(',')
        .map((item) => item.trim())
        .filter(Boolean),
      params: model.params,
      source: model.source,
      source_ref: model.source_ref,
      deprecated: deprecatedBox.input.checked,
      request: {
        method: methodSelect.value,
        path: pathInput.value.trim() || '/',
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
    };
  }

  return { node, read, focus: () => nameInput.focus() };
}

export async function render(ctx) {
  const pid = ctx.pid;
  const view = {
    q: '',
    method: '',
    folder: '',
    tag: '',
    limit: PAGE_SIZE,
    offset: 0,
    items: [],
    total: 0,
    folders: { endpoint_folders: [], tags: [] },
  };

  const main = ctx.container;
  clear(main);
  main.appendChild(
    el(
      'div',
      { class: 'page-head' },
      el('h1', { text: '接口管理' }),
      el('button', {
        type: 'button',
        class: 'btn btn-primary',
        on: { click: () => openEditor(null) },
      }, icon('i-plus'), '新建接口'),
      el('div', { class: 'page-sub', text: '接口目录来自手动创建或导入器解析；可生成冒烟用例并直接试跑。' })
    )
  );

  const qInput = textControl({
    id: 'endpoint-q',
    value: view.q,
    placeholder: '按名称 / 路径 / 摘要搜索',
    on: { input: (event) => { view.q = event.target.value; } },
  });
  const methodFilter = selectControl(
    [{ value: '', label: '全部方法' }].concat(enumOf('http_methods').map((value) => ({ value, label: value }))),
    { id: 'endpoint-filter-method', value: '', on: { change: (event) => apply({ method: event.target.value }) } }
  );
  const folderFilter = selectControl([{ value: '', label: '全部目录' }], {
    id: 'endpoint-filter-folder',
    value: '',
    on: { change: (event) => apply({ folder: event.target.value }) },
  });
  const tagFilter = selectControl([{ value: '', label: '全部标签' }], {
    id: 'endpoint-filter-tag',
    value: '',
    on: { change: (event) => apply({ tag: event.target.value }) },
  });

  const searchForm = el(
    'form',
    { class: 'filter-bar', role: 'search', novalidate: true },
    formField('搜索', qInput, { id: 'endpoint-q' }),
    formField('方法', methodFilter, { id: 'endpoint-filter-method' }),
    formField('目录', folderFilter, { id: 'endpoint-filter-folder' }),
    formField('标签', tagFilter, { id: 'endpoint-filter-tag' }),
    el(
      'div',
      { class: 'row' },
      el('button', { type: 'submit', class: 'btn' }, icon('i-search'), '查询'),
      el('button', {
        type: 'button',
        class: 'btn btn-ghost',
        text: '重置',
        on: {
          click: () => {
            qInput.value = '';
            methodFilter.value = '';
            folderFilter.value = '';
            tagFilter.value = '';
            apply({ q: '', method: '', folder: '', tag: '' });
          },
        },
      })
    )
  );
  searchForm.addEventListener('submit', (event) => {
    event.preventDefault();
    apply({ q: qInput.value.trim() });
  });

  const tableHost = el('div');
  const pagerHost = el('div', { class: 'row', style: { padding: '10px 14px' } });
  const card = el(
    'section',
    { class: 'card' },
    searchForm,
    tableHost,
    pagerHost
  );
  main.appendChild(card);

  function apply(patch) {
    Object.assign(view, patch, { offset: 0 });
    load();
  }

  function setFolderOptions() {
    const folders = view.folders.endpoint_folders || [];
    const current = folderFilter.value;
    clear(folderFilter);
    folderFilter.appendChild(el('option', { value: '', text: '全部目录' }));
    folders.forEach((folder) => folderFilter.appendChild(el('option', { value: folder, text: folder })));
    folderFilter.value = folders.indexOf(current) >= 0 ? current : '';
    view.folder = folderFilter.value;

    const tags = view.folders.tags || [];
    const currentTag = tagFilter.value;
    clear(tagFilter);
    tagFilter.appendChild(el('option', { value: '', text: '全部标签' }));
    tags.forEach((tag) => tagFilter.appendChild(el('option', { value: tag, text: tag })));
    tagFilter.value = tags.indexOf(currentTag) >= 0 ? currentTag : '';
    view.tag = tagFilter.value;
  }

  async function loadFolders() {
    const [ok, data] = await runAction(() => Api.listFolders(pid, { signal: ctx.signal }));
    if (ok && data) {
      view.folders = {
        endpoint_folders: data.endpoint_folders || [],
        tags: data.tags || [],
      };
      setFolderOptions();
    }
  }

  async function load() {
    clear(tableHost);
    tableHost.appendChild(loadingBlock('正在加载接口列表…'));
    let data;
    try {
      data = await Api.listEndpoints(
        pid,
        {
          q: view.q,
          method: view.method,
          folder: view.folder,
          tag: view.tag,
          limit: view.limit,
          offset: view.offset,
        },
        { signal: ctx.signal }
      );
    } catch (err) {
      clear(tableHost);
      tableHost.appendChild(errorBanner(err));
      return;
    }
    if (ctx.signal.aborted) return;
    view.items = itemsOf(data);
    view.total = totalOf(data);
    renderTable();
  }

  function renderTable() {
    clear(tableHost);
    if (!view.items.length) {
      tableHost.appendChild(emptyBlock('没有匹配的接口', '调整筛选条件，或点击「新建接口」/ 前往「导入」。'));
      renderPager();
      return;
    }
    const rows = view.items.map(
      (item) => html`<tr>
        <td class="nowrap">${methodChip(item.method)}</td>
        <td>
          <button type="button" class="link-btn" data-action="detail" data-id="${item.id}">${item.name || item.path}</button>
          <div class="faint">${item.summary || ''}</div>
        </td>
        <td class="mono break-all">${item.path}</td>
        <td>${item.folder || '—'}</td>
        <td>${(item.tags || []).map((tag) => html`<span class="tag">${tag}</span>`)}</td>
        <td class="nowrap">${item.source || 'manual'}${item.deprecated ? html`<span class="chip chip-incomplete">已废弃</span>` : ''}</td>
        <td class="nowrap">${formatDateTime(item.updated_at || item.created_at)}</td>
        <td class="actions">
          <button type="button" class="btn btn-sm" data-action="detail" data-id="${item.id}">详情</button>
          <button type="button" class="btn btn-sm" data-action="edit" data-id="${item.id}">编辑</button>
          <button type="button" class="btn btn-sm" data-action="to-case" data-id="${item.id}">生成用例</button>
          <button type="button" class="btn btn-sm" data-action="probe" data-id="${item.id}">试跑</button>
          <button type="button" class="btn btn-sm btn-danger" data-action="delete" data-id="${item.id}">删除</button>
        </td>
      </tr>`
    );
    const table = el('table', { class: 'data' });
    mount(
      table,
      html`<thead>
        <tr>
          <th>方法</th>
          <th>名称 / 摘要</th>
          <th>路径</th>
          <th>目录</th>
          <th>标签</th>
          <th>来源</th>
          <th>更新时间</th>
          <th class="nowrap">操作</th>
        </tr>
      </thead>
      <tbody>
        ${rows}
      </tbody>`
    );
    table.addEventListener('click', (event) => {
      const button = event.target.closest('[data-action]');
      if (!button) return;
      const id = Number(button.dataset.id);
      const item = view.items.find((entry) => Number(entry.id) === id);
      if (!item) return;
      const action = button.dataset.action;
      if (action === 'detail') openDetail(item);
      else if (action === 'edit') openEditor(item);
      else if (action === 'to-case') openToCase(item);
      else if (action === 'probe') openProbe(item);
      else if (action === 'delete') removeEndpoint(item);
    });
    tableHost.appendChild(el('div', { class: 'table-wrap' }, table));
    renderPager();
  }

  function renderPager() {
    clear(pagerHost);
    const from = view.total ? view.offset + 1 : 0;
    const to = Math.min(view.offset + view.items.length, view.total);
    pagerHost.appendChild(
      el('span', { class: 'muted', text: '第 ' + from + '–' + to + ' 条 / 共 ' + view.total + ' 条' })
    );
    pagerHost.appendChild(el('div', { class: 'spacer' }));
    pagerHost.appendChild(
      el('button', {
        type: 'button',
        class: 'btn btn-sm',
        text: '上一页',
        disabled: view.offset <= 0,
        on: {
          click: () => {
            view.offset = Math.max(0, view.offset - view.limit);
            load();
          },
        },
      })
    );
    pagerHost.appendChild(
      el('button', {
        type: 'button',
        class: 'btn btn-sm',
        text: '下一页',
        disabled: view.offset + view.limit >= view.total,
        on: {
          click: () => {
            view.offset += view.limit;
            load();
          },
        },
      })
    );
  }

  function openDetail(item) {
    const body = el(
      'div',
      { class: 'stack' },
      el(
        'div',
        { class: 'detail-grid' },
        el('div', { class: 'detail-item' }, el('dt', { text: '接口 ID' }), el('dd', { class: 'mono', text: String(item.id) })),
        el('div', { class: 'detail-item' }, el('dt', { text: '方法' }), el('dd', {}, methodChip(item.method))),
        el('div', { class: 'detail-item' }, el('dt', { text: '路径' }), el('dd', { class: 'mono break-all', text: item.path })),
        el('div', { class: 'detail-item' }, el('dt', { text: '目录' }), el('dd', { text: item.folder || '—' })),
        el('div', { class: 'detail-item' }, el('dt', { text: '来源 source' }), el('dd', { class: 'mono', text: item.source || '—' })),
        el('div', { class: 'detail-item' }, el('dt', { text: '来源引用 source_ref' }), el('dd', { class: 'mono break-all', text: item.source_ref || '—' })),
        el('div', { class: 'detail-item' }, el('dt', { text: 'operation_id' }), el('dd', { class: 'mono break-all', text: item.operation_id || '—' })),
        el('div', { class: 'detail-item' }, el('dt', { text: '更新时间' }), el('dd', { text: formatDateTime(item.updated_at) }))
      ),
      item.summary ? el('p', { class: 'muted', text: item.summary }) : null,
      item.description ? el('pre', { text: item.description }) : null,
      el('h3', { text: '参数元数据 params' }),
      paramsTable(item.params),
      el('h3', { text: '请求模板 request' }),
      requestPreviewBlock(item.request),
      kvTable('Query', (item.request || {}).query),
      kvTable('Headers', (item.request || {}).headers),
      kvTable('Cookies', (item.request || {}).cookies),
      el('div', { class: 'field-hint', text: '请求体类型：' + ((item.request || {}).body_type || 'none') }),
      (item.request || {}).body === null || (item.request || {}).body === undefined
        ? null
        : el('pre', { text: typeof (item.request || {}).body === 'string' ? item.request.body : formatJson((item.request || {}).body) })
    );

    const handle = openDrawer({
      title: item.name || item.path,
      wide: true,
      content: body,
      footer: [
        el('button', {
          type: 'button',
          class: 'btn btn-danger',
          text: '删除',
          on: {
            click: () => {
              handle.close();
              removeEndpoint(item);
            },
          },
        }),
        el('div', { class: 'spacer' }),
        el('button', {
          type: 'button',
          class: 'btn',
          text: '生成用例',
          on: {
            click: () => {
              handle.close();
              openToCase(item);
            },
          },
        }),
        el('button', {
          type: 'button',
          class: 'btn',
          text: '试跑',
          on: {
            click: () => {
              handle.close();
              openProbe(item);
            },
          },
        }),
        el('button', {
          type: 'button',
          class: 'btn btn-primary',
          text: '编辑',
          on: {
            click: () => {
              handle.close();
              openEditor(item);
            },
          },
        }),
      ],
    });
  }

  function openEditor(item) {
    const form = endpointForm(item || {}, {});
    const handle = openDrawer({
      title: item ? '编辑接口 #' + item.id : '新建接口',
      wide: true,
      content: form.node,
      footer: [
        el('div', { class: 'spacer' }),
        el('button', { type: 'button', class: 'btn', text: '取消', on: { click: () => handle.close() } }),
        el('button', {
          type: 'button',
          class: 'btn btn-primary',
          text: item ? '保存修改' : '创建接口',
          on: {
            click: async () => {
              let payload;
              try {
                payload = form.read();
              } catch (err) {
                toast(err.message, 'error');
                return;
              }
              if (!payload.name) {
                toast('接口名称不能为空', 'error');
                return;
              }
              if (!payload.path) {
                toast('路径不能为空', 'error');
                return;
              }
              const [ok] = await runAction(
                () => (item ? Api.updateEndpoint(pid, item.id, payload) : Api.createEndpoint(pid, payload)),
                item ? '接口已更新' : '接口已创建'
              );
              if (ok) {
                handle.close();
                loadFolders();
                load();
              }
            },
          },
        }),
      ],
    });
  }

  function openToCase(item) {
    const nameInput = textControl({
      id: 'to-case-name',
      value: '',
      placeholder: (item.name || item.path) + ' - 冒烟',
    });
    const assertionsBox = checkboxControl({
      id: 'to-case-assertions',
      label: '自动生成断言（generate_assertions）',
      checked: true,
    });
    const handle = openModal({
      title: '由接口生成用例',
      content: el(
        'div',
        { class: 'stack' },
        el('p', {
          class: 'muted',
          text: '将按接口模板生成一条冒烟用例：' + item.method + ' ' + item.path,
        }),
        formField('用例名称', nameInput, { id: 'to-case-name', hint: '留空则由后端按接口名生成' }),
        assertionsBox.node
      ),
      footer: [
        el('div', { class: 'spacer' }),
        el('button', { type: 'button', class: 'btn', text: '取消', on: { click: () => handle.close() } }),
        el('button', {
          type: 'button',
          class: 'btn btn-primary',
          text: '生成',
          on: {
            click: async () => {
              const payload = { generate_assertions: assertionsBox.input.checked };
              const name = nameInput.value.trim();
              if (name) payload.name = name;
              const [ok, data] = await runAction(() => Api.endpointToCase(pid, item.id, payload), '用例已生成');
              if (!ok) return;
              handle.close();
              const created = (data && (data.case || data.case_spec || data)) || {};
              if (created && created.id) {
                toast('已生成用例「' + (created.name || created.id) + '」', 'success');
                ctx.navigate('#/p/' + encodeURIComponent(pid) + '/cases/' + encodeURIComponent(created.id));
              } else {
                toast('用例已生成，请在「用例管理」中查看', 'success');
              }
            },
          },
        }),
      ],
    });
  }

  async function openProbe(item) {
    const handle = openModal({
      title: '试跑接口 ' + item.method + ' ' + item.path,
      wide: true,
      content: loadingBlock('正在发送请求…'),
      footer: [el('div', { class: 'spacer' }), el('button', { type: 'button', class: 'btn', text: '关闭', on: { click: () => handle.close() } })],
    });
    let response;
    try {
      response = await Api.probeEndpoint(pid, item.id, {});
    } catch (err) {
      clear(handle.body);
      handle.body.appendChild(errorBanner(err));
      return;
    }
    if (!handle.root.parentNode) return;
    const data = response || {};
    clear(handle.body);
    handle.body.appendChild(
      el(
        'div',
        { class: 'stack' },
        el(
          'div',
          { class: 'row' },
          data.ok ? statusChip('passed') : statusChip('error'),
          el('span', { class: 'mono', text: 'HTTP ' + (data.status === null || data.status === undefined ? '—' : data.status) }),
          el('span', { class: 'muted', text: formatDuration(data.duration_ms) }),
          el('span', { class: 'faint', text: (data.method || item.method) + ' ' + (data.url || '') })
        ),
        data.error ? el('div', { class: 'banner banner-error' }, icon('i-alert'), el('span', { text: data.error })) : null,
        el('div', {
          class: 'field-hint',
          text:
            '响应大小 ' +
            String(data.size_bytes || 0) +
            ' 字节' +
            (data.truncated ? '（已按配置截断）' : '') +
            ' · 试跑不写入运行历史，展示内容已由后端脱敏',
        }),
        el('h3', { text: '响应头' }),
        data.headers && Object.keys(data.headers).length
          ? el(
              'dl',
              { class: 'kv-list' },
              Object.keys(data.headers).reduce(
                (acc, key) => acc.concat([el('dt', { text: key }), el('dd', { text: String(data.headers[key]) })]),
                []
              )
            )
          : el('div', { class: 'field-hint', text: '无响应头' }),
        el('h3', { text: '响应体预览' }),
        el('pre', {
          text: data.body_json !== null && data.body_json !== undefined
            ? formatJson(data.body_json)
            : data.body_text || '（空响应体）',
        })
      )
    );
  }

  async function removeEndpoint(item) {
    const confirmed = await confirmDialog({
      title: '删除接口',
      message: '将删除接口「' + (item.name || item.path) + '」（' + item.method + ' ' + item.path + '）。已生成的用例不会被删除。',
      confirmLabel: '删除接口',
    });
    if (!confirmed) return;
    const [ok] = await runAction(() => Api.deleteEndpoint(pid, item.id), '接口已删除');
    if (ok) {
      if (view.items.length === 1 && view.offset >= view.limit) view.offset -= view.limit;
      loadFolders();
      load();
    }
  }

  await loadFolders();
  await load();
}
