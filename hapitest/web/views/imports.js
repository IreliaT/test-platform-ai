/**
 * 导入：POST /api/v1/projects/{pid}/imports/preview → POST …/imports
 * 三种来源：粘贴文本 / 上传文件（浏览器只能提交 text，不能提交服务端 path）/ 从 URL 抓取（POST …/imports/fetch）。
 * 另展示导入批次历史 GET …/imports。
 */

import { Api } from '../api.js';
import {
  checkboxControl,
  clear,
  el,
  emptyBlock,
  enumOf,
  errorBanner,
  formField,
  formatDateTime,
  html,
  icon,
  itemsOf,
  loadingBlock,
  mount,
  runAction,
  selectControl,
  textControl,
  textareaControl,
  toast,
  totalOf,
} from '../app.js';

const SOURCE_LABELS = {
  auto: 'auto（自动识别）',
  openapi: 'openapi（OpenAPI 3）',
  swagger2: 'swagger2（Swagger 2.0）',
  postman: 'postman（Postman Collection）',
  apifox: 'apifox（Apifox 导出）',
  curl: 'curl（curl 命令）',
  spring: 'spring（Spring 源码扫描，服务端路径）',
};

const MODE_LABELS = {
  merge: 'merge：按 method + path 更新已有接口，保留用例',
  replace: 'replace：先清空接口目录再导入（用例外键置空）',
};

function statChips(stats) {
  const data = stats || {};
  return el(
    'div',
    { class: 'row' },
    el('span', { class: 'chip chip-passed', text: '新增 ' + (data.created === undefined ? 0 : data.created) }),
    el('span', { class: 'chip chip-queued', text: '更新 ' + (data.updated === undefined ? 0 : data.updated) }),
    el('span', { class: 'chip', text: '未变化 ' + (data.unchanged === undefined ? 0 : data.unchanged) }),
    el('span', { class: 'chip chip-skipped', text: '跳过 ' + (data.skipped === undefined ? 0 : data.skipped) }),
    el('span', { class: 'chip chip-incomplete', text: '生成用例 ' + (data.cases_created === undefined ? 0 : data.cases_created) }),
    data.source ? el('span', { class: 'chip', text: '来源 ' + data.source }) : null
  );
}

function warningsBlock(warnings) {
  const list = Array.isArray(warnings) ? warnings : [];
  if (!list.length) return el('div', { class: 'banner banner-ok' }, icon('i-check'), el('span', { text: '解析无警告。' }));
  return el(
    'div',
    { class: 'banner banner-warn' },
    icon('i-alert'),
    el(
      'div',
      {},
      el('div', { text: '解析警告（' + list.length + ' 条）' }),
      el('ul', {}, list.map((item) => el('li', { text: String(item) })))
    )
  );
}

function previewBlock(preview) {
  const data = preview || {};
  const endpoints = Array.isArray(data.endpoints) ? data.endpoints : [];
  const cases = Array.isArray(data.cases) ? data.cases : [];
  const host = el('div', { class: 'stack' });
  host.appendChild(
    el(
      'div',
      { class: 'row' },
      data.title ? el('span', { class: 'chip chip-queued', text: '文档：' + data.title }) : null,
      data.version ? el('span', { class: 'chip', text: '版本 ' + data.version }) : null,
      el('span', { class: 'chip', text: '接口 ' + endpoints.length }),
      el('span', { class: 'chip', text: '用例 ' + cases.length })
    )
  );
  host.appendChild(statChips(data.stats));
  host.appendChild(warningsBlock((data.stats || {}).warnings));

  if (!endpoints.length) {
    host.appendChild(emptyBlock('没有解析到接口', '请检查文本内容或来源类型是否正确。'));
    return host;
  }
  const rows = endpoints.map(
    (item) => html`<tr>
      <td class="nowrap"><span class="method method-${item.method}">${item.method}</span></td>
      <td class="mono break-all">${item.path}</td>
      <td>${item.name || '—'}</td>
      <td>${item.folder || '—'}</td>
      <td class="nowrap">${item.deprecated ? html`<span class="chip chip-incomplete">已废弃</span>` : ''}</td>
    </tr>`
  );
  const table = el('table', { class: 'data' });
  mount(
    table,
    html`<caption class="muted">解析到 ${endpoints.length} 个接口（预览不会写库）</caption>
    <thead>
      <tr>
        <th>方法</th>
        <th>路径</th>
        <th>名称</th>
        <th>目录</th>
        <th>状态</th>
      </tr>
    </thead>
    <tbody>
      ${rows}
    </tbody>`
  );
  host.appendChild(el('div', { class: 'table-wrap', style: { maxHeight: '360px', overflow: 'auto' } }, table));
  return host;
}

function batchRow(batch) {
  const data = batch || {};
  const id = data.batch_id || data.id || '—';
  const time = data.created_at || data.imported_at || data.time || data.started_at || '';
  const stats = data.stats || data;
  return el(
    'tr',
    {},
    el('td', { class: 'mono', text: String(id) }),
    el('td', { class: 'nowrap', text: formatDateTime(time) }),
    el('td', { text: data.source || stats.source || '—' }),
    el('td', { text: data.mode || '—' }),
    el('td', { class: 'nowrap', text: String(stats.created === undefined ? '—' : stats.created) }),
    el('td', { class: 'nowrap', text: String(stats.updated === undefined ? '—' : stats.updated) }),
    el('td', { class: 'nowrap', text: String(stats.cases_created === undefined ? '—' : stats.cases_created) }),
    el('td', { class: 'nowrap', text: String(stats.skipped === undefined ? '—' : stats.skipped) }),
    el('td', {
      class: 'muted',
      text: Array.isArray(stats.warnings) && stats.warnings.length ? stats.warnings.length + ' 条警告' : '—',
    })
  );
}

export async function render(ctx) {
  const pid = ctx.pid;
  const view = {
    tab: 'text',
    source: 'auto',
    mode: 'merge',
    text: '',
    url: '',
    fileName: '',
    fileText: '',
    preview: null,
    previewSource: null,
    fetched: null,
    result: null,
    batches: [],
    baseUrl: '',
    folderPrefix: '',
    generateCases: true,
    caseSuffix: ' - 冒烟',
    includeDeprecated: false,
    tags: '',
    previewHost: null,
    resultHost: null,
    confirmButton: null,
  };

  const main = ctx.container;
  clear(main);
  main.appendChild(
    el(
      'div',
      { class: 'page-head' },
      el('h1', { text: '导入' }),
      el('div', {
        class: 'page-sub',
        text: '支持 OpenAPI / Swagger2 / Postman / Apifox / curl（Spring 源码扫描需服务端路径，请在 CLI 或文本方式提供内容）。先预览再导入。',
      })
    )
  );

  /* ---------------------------------------------------------- 通用选项 */

  const sourceSelect = selectControl(
    enumOf('import_sources').map((value) => ({ value, label: SOURCE_LABELS[value] || value })),
    {
      id: 'import-source',
      value: view.source,
      on: {
        change: (event) => {
          view.source = event.target.value;
        },
      },
    }
  );
  const modeSelect = selectControl(
    [
      { value: 'merge', label: MODE_LABELS.merge },
      { value: 'replace', label: MODE_LABELS.replace },
    ],
    {
      id: 'import-mode',
      value: view.mode,
      on: {
        change: (event) => {
          view.mode = event.target.value;
        },
      },
    }
  );
  const baseUrlInput = textControl({
    id: 'import-base-url',
    placeholder: 'http://127.0.0.1:8080（留空则用文档 servers 或项目 base_url）',
    on: { input: (event) => { view.baseUrl = event.target.value; } },
  });
  const folderPrefixInput = textControl({
    id: 'import-folder-prefix',
    placeholder: '可选，统一加到目录前，如 v1/',
    on: { input: (event) => { view.folderPrefix = event.target.value; } },
  });
  const caseSuffixInput = textControl({
    id: 'import-case-suffix',
    value: view.caseSuffix,
    on: { input: (event) => { view.caseSuffix = event.target.value; } },
  });
  const generateBox = checkboxControl({
    id: 'import-generate-cases',
    label: '为每个接口生成冒烟用例',
    checked: view.generateCases,
    on: { change: (event) => { view.generateCases = event.target.checked; } },
  });
  const deprecatedBox = checkboxControl({
    id: 'import-include-deprecated',
    label: '包含 deprecated 接口',
    checked: view.includeDeprecated,
    on: { change: (event) => { view.includeDeprecated = event.target.checked; } },
  });
  const tagsInput = textControl({
    id: 'import-tags',
    placeholder: '导入后统一附加标签，逗号分隔',
    on: { input: (event) => { view.tags = event.target.value; } },
  });

  const optionsCard = el(
    'section',
    { class: 'card section' },
    el('div', { class: 'card-head' }, el('h2', { text: '导入选项' })),
    el(
      'div',
      { class: 'card-body stack' },
      el(
        'div',
        { class: 'grid-3' },
        formField('来源 source', sourceSelect, { id: 'import-source' }),
        formField('写入模式 mode', modeSelect, { id: 'import-mode' }),
        formField('base_url', baseUrlInput, { id: 'import-base-url' })
      ),
      el(
        'div',
        { class: 'grid-3' },
        formField('目录前缀 folder_prefix', folderPrefixInput, { id: 'import-folder-prefix' }),
        formField('用例后缀 case_suffix', caseSuffixInput, { id: 'import-case-suffix' }),
        formField('标签 tags', tagsInput, { id: 'import-tags' })
      ),
      el('div', { class: 'row' }, generateBox.node, deprecatedBox.node)
    )
  );
  main.appendChild(optionsCard);

  /* ---------------------------------------------------------- 三个标签页 */

  const textArea = textareaControl({
    id: 'import-text',
    rows: 12,
    placeholder: '粘贴 OpenAPI JSON/YAML、Postman collection、curl 命令…',
    on: { input: (event) => { view.text = event.target.value; } },
  });

  const fileInput = el('input', {
    type: 'file',
    id: 'import-file',
    accept: '.json,.yaml,.yml,.txt,.har,.md',
    on: {
      change: async (event) => {
        const file = event.target.files && event.target.files[0];
        if (!file) return;
        if (file.size > 8 * 1024 * 1024) {
          toast('文件超过 8MB，请改用命令行导入', 'error');
          event.target.value = '';
          return;
        }
        try {
          view.fileText = await file.text();
          view.fileName = file.name;
          fileNameNode.textContent = '已读取：' + file.name + '（' + view.fileText.length + ' 字符）';
        } catch (err) {
          toast('文件读取失败：' + err.message, 'error');
        }
      },
    },
  });
  const fileNameNode = el('div', { class: 'field-hint', text: '尚未选择文件' });

  const urlInput = textControl({
    id: 'import-url',
    placeholder: 'http://127.0.0.1:8080/v3/api-docs',
    on: { input: (event) => { view.url = event.target.value; } },
  });
  const fetchButton = el(
    'button',
    {
      type: 'button',
      class: 'btn',
      on: {
        click: async () => {
          const url = urlInput.value.trim();
          if (!url) {
            toast('请输入文档 URL', 'error');
            return;
          }
          fetchButton.disabled = true;
          const [ok, data] = await runAction(() => Api.fetchImport(pid, buildBody({ url }, false)));
          fetchButton.disabled = false;
          if (!ok) return;
          view.fetched = data || {};
          view.url = url;
          setPreview((data && data.preview) || null, 'url');
          toast('已抓取 ' + ((data && data.bytes) || 0) + ' 字节并完成解析', 'success');
        },
      },
    },
    icon('i-link'),
    '抓取并解析'
  );

  const apifoxProjectInput = el('input', {
    type: 'text',
    id: 'mcp-apifox-project',
    placeholder: '例如 1234567（Apifox 项目地址栏 / 项目设置里可见）',
    value: view.apifoxProject || '',
    on: { input: (event) => { view.apifoxProject = event.target.value; } },
  });
  const apifoxDocInput = el('input', {
    type: 'text',
    id: 'mcp-apifox-doc',
    placeholder: '可选：公开发布文档的 ID（与项目 ID 二选一）',
    value: view.apifoxDoc || '',
    on: { input: (event) => { view.apifoxDoc = event.target.value; } },
  });
  const apifoxBranchInput = el('input', {
    type: 'text',
    id: 'mcp-apifox-branch',
    placeholder: '可选：迭代分支 ID（不填读主分支）',
    value: view.apifoxBranch || '',
    on: { input: (event) => { view.apifoxBranch = event.target.value; } },
  });
  const mcpRefreshBox = checkboxControl({
    id: 'mcp-refresh',
    label: '同步前强制刷新 Apifox 缓存',
    checked: view.mcpRefresh !== false,
    on: { change: (event) => { view.mcpRefresh = event.target.checked; } },
  });
  const mcpSyncButton = el(
    'button',
    {
      type: 'button',
      class: 'btn',
      on: {
        click: async () => {
          mcpSyncButton.disabled = true;
          try {
            await doMcpPreview();
          } finally {
            mcpSyncButton.disabled = false;
          }
        },
      },
    },
    icon('i-refresh'),
    '从 Apifox 同步并预览'
  );

  const panels = {
    text: el(
      'div',
      { class: 'stack', role: 'tabpanel', id: 'panel-text', 'aria-labelledby': 'tab-text' },
      formField('文档内容', textArea, { id: 'import-text', hint: 'YAML 需要可解析为 OpenAPI/Swagger 文档' })
    ),
    file: el(
      'div',
      { class: 'stack', role: 'tabpanel', id: 'panel-file', 'aria-labelledby': 'tab-file' },
      el('div', { class: 'field' }, el('label', { for: 'import-file' }, '选择文件'), fileInput, fileNameNode),
      el('div', {
        class: 'field-hint',
        text: '浏览器无法提供服务端文件路径（path 字段仅供 CLI/服务端使用），上传内容以 text 字段提交。',
      })
    ),
    mcp: el(
      'div',
      { class: 'stack', role: 'tabpanel', id: 'panel-mcp', 'aria-labelledby': 'tab-mcp' },
      el(
        'div',
        { class: 'row' },
        el('div', { style: { flex: '1', minWidth: '240px' } }, formField('Apifox 项目 ID', apifoxProjectInput, { id: 'mcp-apifox-project' })),
        el('div', { style: { flex: '1', minWidth: '240px' } }, formField('Apifox 文档 ID', apifoxDocInput, { id: 'mcp-apifox-doc' })),
        el('div', { style: { flex: '1', minWidth: '200px' } }, formField('分支 ID', apifoxBranchInput, { id: 'mcp-apifox-branch' }))
      ),
      mcpRefreshBox.node,
      el('div', { class: 'row' }, mcpSyncButton),
      el('div', {
        class: 'field-hint',
        text: '通过 Apifox MCP Server（npx apifox-mcp-server）读取接口文档，无需手工导出文件。访问令牌只在服务端读取：把 APIFOX_ACCESS_TOKEN=xxx 写进项目根目录的 .env.local（已排除在版本库外），页面不会也无需填写令牌。',
      }),
      view.mcpMeta
        ? el('div', {
            class: 'field-hint',
            text: '本次同步：' + (view.mcpMeta.server || 'MCP') + ' · 分支 ' + (view.mcpMeta.branch_id || '(主分支)') + ' · 工具 ' + (view.mcpMeta.tool || '-') + ' · ' + (view.mcpMeta.refreshed ? '已刷新' : '使用缓存') + ' · ' + (view.mcpMeta.bytes || 0) + ' 字节',
          })
        : el('div', { class: 'field-hint', text: '首次使用需要本机有 Node.js 18+（npx 随 Node 安装）。' })
    ),
    url: el(
      'div',
      { class: 'stack', role: 'tabpanel', id: 'panel-url', 'aria-labelledby': 'tab-url' },
      el(
        'div',
        { class: 'row' },
        el('div', { style: { flex: '1', minWidth: '260px' } }, formField('文档 URL', urlInput, { id: 'import-url' })),
        fetchButton
      ),
      view.fetched
        ? el('div', {
            class: 'field-hint',
            text: '已抓取：' + (view.fetched.url || '') + ' · ' + (view.fetched.bytes || 0) + ' 字节',
          })
        : el('div', { class: 'field-hint', text: '抓取只是预览；点「确认导入」时会按 url 重新抓取并写库。' })
    ),
  };

  const tabButtons = [
    { key: 'text', label: '粘贴文本' },
    { key: 'file', label: '上传文件' },
    { key: 'url', label: '从 URL 抓取' },
    { key: 'mcp', label: 'Apifox（MCP）' },
  ].map((tab) =>
    el('button', {
      type: 'button',
      class: 'tab',
      id: 'tab-' + tab.key,
      role: 'tab',
      'aria-selected': tab.key === view.tab ? 'true' : 'false',
      'aria-controls': 'panel-' + tab.key,
      text: tab.label,
      on: {
        click: () => setTab(tab.key),
        keydown: (event) => {
          if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft') return;
          const keys = ['text', 'file', 'url'];
          const index = keys.indexOf(view.tab);
          const next = keys[(index + (event.key === 'ArrowRight' ? 1 : keys.length - 1)) % keys.length];
          setTab(next);
          document.getElementById('tab-' + next).focus();
        },
      },
    })
  );

  function setTab(key) {
    view.tab = key;
    tabButtons.forEach((button) => {
      const active = button.id === 'tab-' + key;
      button.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    Object.keys(panels).forEach((panelKey) => {
      panels[panelKey].hidden = panelKey !== key;
    });
    setResult(null);
    // 切换来源后旧的预览结果不再对应当前输入
    view.preview = null;
    view.confirmButton.disabled = true;
    clear(view.previewHost);
    view.previewHost.appendChild(
      el('div', { class: 'field-hint', text: '还没有预览结果；点击「预览解析结果」只解析不写库。' })
    );
  }

  const sourceCard = el(
    'section',
    { class: 'card section' },
    el('div', { class: 'card-head' }, el('h2', { text: '文档来源' })),
    el(
      'div',
      { class: 'card-body stack' },
      el('div', { class: 'tabs', role: 'tablist' }, tabButtons),
      panels.text,
      panels.file,
      panels.url,
      panels.mcp
    )
  );
  main.appendChild(sourceCard);

  /* ---------------------------------------------------------- 预览与导入 */

  view.previewHost = el('div');
  view.resultHost = el('div');
  const previewCard = el(
    'section',
    { class: 'card section' },
    el(
      'div',
      { class: 'card-head' },
      el('h2', { text: '解析预览' }),
      el('div', { class: 'spacer' }),
      el(
        'button',
        {
          type: 'button',
          class: 'btn',
          on: { click: () => doPreview() },
        },
        icon('i-search'),
        '预览解析结果'
      ),
      (view.confirmButton = el(
        'button',
        {
          type: 'button',
          class: 'btn btn-primary',
          disabled: true,
          on: { click: () => doImport() },
        },
        icon('i-check'),
        '确认导入'
      ))
    ),
    el('div', { class: 'card-body stack' }, view.previewHost, view.resultHost)
  );
  main.appendChild(previewCard);
  view.previewHost.appendChild(el('div', { class: 'field-hint', text: '还没有预览结果；点击「预览解析结果」只解析不写库。' }));

  function currentPayloadSource() {
    if (view.tab === 'text') return { text: view.text };
    if (view.tab === 'file') return { text: view.fileText };
    if (view.tab === 'mcp') {
      const payload = { apifox_project_id: (apifoxProjectInput.value || '').trim() };
      const docId = (apifoxDocInput.value || '').trim();
      if (docId) payload.doc_id = docId;
      const branchId = (apifoxBranchInput.value || '').trim();
      if (branchId) payload.branch_id = branchId;
      payload.refresh = view.mcpRefresh !== false;
      return payload;
    }
    return { url: urlInput.value.trim() || view.url };
  }

  function buildBody(extra, includeMode) {
    const tags = view.tags
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean);
    const body = Object.assign(
      {
        source: sourceSelect.value || 'auto',
        options: {
          base_url: baseUrlInput.value.trim(),
          folder_prefix: folderPrefixInput.value.trim(),
          generate_cases: generateBox.input.checked,
          case_suffix: caseSuffixInput.value,
          include_deprecated: deprecatedBox.input.checked,
          tags,
        },
      },
      currentPayloadSource(),
      extra || {}
    );
    if (includeMode !== false) body.mode = modeSelect.value || 'merge';
    return body;
  }

  function setPreview(preview, sourceLabel) {
    view.preview = preview;
    view.previewSource = sourceLabel;
    clear(view.previewHost);
    if (!preview) {
      view.previewHost.appendChild(el('div', { class: 'field-hint', text: '解析失败或没有内容。' }));
      view.confirmButton.disabled = true;
      return;
    }
    view.previewHost.appendChild(previewBlock(preview));
    view.confirmButton.disabled = false;
  }

  function setResult(result) {
    view.result = result;
    clear(view.resultHost);
    if (!result) return;
    const stats = result.stats || {};
    view.resultHost.appendChild(
      el(
        'div',
        { class: 'banner banner-ok' },
        icon('i-check'),
        el(
          'div',
          {},
          el('div', { text: '导入完成' + (result.batch_id ? '（批次 ' + result.batch_id + '）' : '') }),
          el('div', { class: 'row' }, statChips(stats)),
          el(
            'div',
            { class: 'row' },
            el('a', { class: 'btn btn-sm', href: '#/p/' + encodeURIComponent(pid) + '/endpoints' }, '查看接口目录'),
            el('a', { class: 'btn btn-sm', href: '#/p/' + encodeURIComponent(pid) + '/cases' }, '查看用例')
          )
        )
      )
    );
    if (stats.warnings && stats.warnings.length) view.resultHost.appendChild(warningsBlock(stats.warnings));
  }

  async function doMcpPreview() {
    const projectId = (apifoxProjectInput.value || '').trim();
    const docId = (apifoxDocInput.value || '').trim();
    if (!projectId && !docId) {
      toast('请填写 Apifox 项目 ID', 'error');
      return;
    }
    clear(view.previewHost);
    view.previewHost.appendChild(loadingBlock('正在通过 MCP 读取 Apifox 接口文档…'));
    view.confirmButton.disabled = true;
    let data;
    try {
      data = await Api.mcpImport(pid, Object.assign(buildBody({ preview: true }, false), currentPayloadSource()));
    } catch (err) {
      clear(view.previewHost);
      view.previewHost.appendChild(errorBanner(err));
      return;
    }
    view.mcpMeta = (data && data.mcp) || null;
    setPreview((data && data.preview) || null, 'mcp');
    if (data && data.preview && data.preview.stats) {
      toast(
        '解析完成：接口 ' + (itemsOf(data.preview.endpoints).length || 0) + ' 个，用例 ' + (itemsOf(data.preview.cases).length || 0) + ' 条',
        'success'
      );
    }
  }

  async function doPreview() {
    const body = buildBody({}, false);
    if (view.tab === 'text' && !view.text.trim()) {
      toast('请先粘贴文档内容', 'error');
      return;
    }
    if (view.tab === 'file' && !view.fileText.trim()) {
      toast('请先选择文件', 'error');
      return;
    }
    if (view.tab === 'url' && !(urlInput.value.trim() || view.url)) {
      toast('请输入文档 URL', 'error');
      return;
    }
    clear(view.previewHost);
    view.previewHost.appendChild(loadingBlock('正在解析…'));
    view.confirmButton.disabled = true;
    let data;
    try {
      data = await Api.previewImport(pid, body);
    } catch (err) {
      clear(view.previewHost);
      view.previewHost.appendChild(errorBanner(err));
      return;
    }
    setPreview(data, view.tab);
    if (data && data.stats) {
      toast(
        '解析完成：接口 ' + (itemsOf(data.endpoints).length || 0) + ' 个，用例 ' + (itemsOf(data.cases).length || 0) + ' 条',
        'success'
      );
    }
  }

  async function doImport() {
    if (!view.preview) {
      toast('请先预览解析结果', 'error');
      return;
    }
    if (view.tab === 'mcp') {
      const body = Object.assign(buildBody({}, true), currentPayloadSource(), { preview: false });
      view.confirmButton.disabled = true;
      const [ok, data] = await runAction(() => Api.mcpImport(pid, body), '导入完成');
      view.confirmButton.disabled = false;
      if (!ok) return;
      view.mcpMeta = (data && data.mcp) || null;
      setResult(data || {});
      await loadBatches();
      return;
    }
    const body = buildBody({}, true);
    view.confirmButton.disabled = true;
    const [ok, data] = await runAction(() => Api.createImport(pid, body), '导入完成');
    view.confirmButton.disabled = false;
    if (!ok) return;
    setResult(data || {});
    await loadBatches();
  }

  /* ---------------------------------------------------------- 批次历史 */

  const historyHost = el('div');
  const historyCard = el(
    'section',
    { class: 'card section' },
    el(
      'div',
      { class: 'card-head' },
      el('h2', { text: '导入批次历史' }),
      el('span', { class: 'pill-count', id: 'import-batch-count' }),
      el('div', { class: 'spacer' }),
      el('button', {
        type: 'button',
        class: 'btn btn-sm',
        on: { click: () => loadBatches() },
      }, icon('i-refresh'), '刷新')
    ),
    historyHost
  );
  main.appendChild(historyCard);

  async function loadBatches() {
    clear(historyHost);
    historyHost.appendChild(loadingBlock('正在加载导入批次…'));
    let data;
    try {
      data = await Api.listImports(pid, {}, { signal: ctx.signal });
    } catch (err) {
      clear(historyHost);
      historyHost.appendChild(errorBanner(err));
      return;
    }
    if (ctx.signal.aborted) return;
    view.batches = itemsOf(data);
    const countNode = document.getElementById('import-batch-count');
    if (countNode) countNode.textContent = String(totalOf(data));
    clear(historyHost);
    if (!view.batches.length) {
      historyHost.appendChild(emptyBlock('还没有导入批次', '完成一次导入后会在这里留下记录。'));
      return;
    }
    const table = el(
      'table',
      { class: 'data' },
      el(
        'thead',
        {},
        el(
          'tr',
          {},
          el('th', { text: '批次' }),
          el('th', { text: '时间' }),
          el('th', { text: '来源' }),
          el('th', { text: '模式' }),
          el('th', { text: '新增' }),
          el('th', { text: '更新' }),
          el('th', { text: '生成用例' }),
          el('th', { text: '跳过' }),
          el('th', { text: '警告' })
        )
      ),
      el(
        'tbody',
        {},
        view.batches.map((batch) => batchRow(batch))
      )
    );
    historyHost.appendChild(el('div', { class: 'table-wrap' }, table));
  }

  setTab('text');
  await loadBatches();
}
