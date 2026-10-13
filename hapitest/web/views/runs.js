/**
 * 运行与历史：POST/GET /api/v1/projects/{pid}/runs
 * 触发面板（套件或自选用例 + 环境 + 备注）+ 运行列表（状态 / 套件筛选、分页、取消、报告下载）。
 */

import { Api, newRequestId } from '../api.js';
import {
  clear,
  el,
  emptyBlock,
  errorBanner,
  formField,
  formatDateTime,
  formatDuration,
  html,
  icon,
  itemsOf,
  loadingBlock,
  mount,
  runAction,
  selectControl,
  statusChip,
  statusLabel,
  statusOptions,
  textControl,
  toast,
  totalOf,
} from '../app.js';

const PAGE_SIZE = 50;
const ACTIVE_REFRESH_MS = 3000;

export async function render(ctx) {
  const pid = ctx.pid;
  const view = {
    runs: [],
    total: 0,
    offset: 0,
    limit: PAGE_SIZE,
    status: '',
    suiteId: '',
    suites: [],
    environments: [],
    cases: [],
    mode: 'suite',
    selectedCaseIds: [],
    caseSearch: '',
    timer: null,
  };

  const main = ctx.container;
  clear(main);
  main.appendChild(
    el(
      'div',
      { class: 'page-head' },
      el('h1', { text: '运行与历史' }),
      el('button', {
        type: 'button',
        class: 'btn',
        on: { click: () => loadRuns() },
      }, icon('i-refresh'), '刷新列表'),
      el('div', { class: 'page-sub', text: '触发一次运行后可进入运行详情查看逐用例结果，或下载 JUnit XML / HTML 报告。' })
    )
  );

  /* -------------------------------------------------------- 触发面板 */

  const suiteSelect = selectControl([{ value: '', label: '请选择套件' }], { id: 'run-suite' });
  const envSelect = selectControl([{ value: '', label: '使用默认环境' }], { id: 'run-env' });
  const noteInput = textControl({ id: 'run-note', placeholder: '本次运行的备注（可选）' });
  const operatorInput = textControl({ id: 'run-operator', placeholder: '执行人（可选）' });

  const caseSearchInput = textControl({
    id: 'run-case-search',
    placeholder: '搜索用例名称 / 路径',
    on: {
      input: (event) => {
        view.caseSearch = event.target.value.trim();
        renderCasePicker();
      },
    },
  });
  const casePickerHost = el('ul', { class: 'picker-list' });
  const selectedCount = el('span', { class: 'pill-count' });

  const suitePane = el(
    'div',
    { class: 'stack' },
    formField('套件', suiteSelect, { id: 'run-suite', hint: '套件按 case_ids 顺序执行' })
  );
  const casesPane = el(
    'div',
    { class: 'stack' },
    formField('搜索用例', caseSearchInput, { id: 'run-case-search', hint: '共 ' + view.cases.length + ' 条用例' }),
    el('div', { class: 'picker' }, casePickerHost),
    el('div', { class: 'row' }, el('span', { class: 'muted', text: '已选 ' }), selectedCount, el('span', { class: 'muted', text: ' 条' }))
  );
  casesPane.hidden = true;

  const modeSuiteButton = el('button', {
    type: 'button',
    class: 'tab',
    role: 'tab',
    'aria-selected': 'true',
    text: '按套件运行',
    on: { click: () => setMode('suite') },
  });
  const modeCasesButton = el('button', {
    type: 'button',
    class: 'tab',
    role: 'tab',
    'aria-selected': 'false',
    text: '自选用例',
    on: { click: () => setMode('cases') },
  });

  function setMode(mode) {
    view.mode = mode;
    modeSuiteButton.setAttribute('aria-selected', mode === 'suite' ? 'true' : 'false');
    modeCasesButton.setAttribute('aria-selected', mode === 'cases' ? 'true' : 'false');
    suitePane.hidden = mode !== 'suite';
    casesPane.hidden = mode !== 'cases';
  }

  const triggerButton = el(
    'button',
    {
      type: 'button',
      class: 'btn btn-primary',
      on: {
        click: async () => {
          const payload = {
            trigger: 'manual',
            note: noteInput.value.trim(),
            operator: operatorInput.value.trim(),
            request_id: newRequestId(),
            background: true,
          };
          if (view.mode === 'suite') {
            if (!suiteSelect.value) {
              toast('请选择要运行的套件', 'error');
              return;
            }
            payload.suite_id = Number(suiteSelect.value);
          } else {
            if (!view.selectedCaseIds.length) {
              toast('请至少选择一条用例', 'error');
              return;
            }
            payload.case_ids = view.selectedCaseIds.slice();
          }
          if (envSelect.value) payload.env_id = Number(envSelect.value);
          triggerButton.disabled = true;
          const [ok, data] = await runAction(() => Api.createRun(pid, payload), '运行已触发');
          triggerButton.disabled = false;
          if (!ok) return;
          const run = (data && (data.run || data.run_result)) || data || {};
          if (run && run.id) {
            ctx.navigate('#/p/' + encodeURIComponent(pid) + '/runs/' + encodeURIComponent(run.id));
          } else {
            loadRuns();
          }
        },
      },
    },
    icon('i-play'),
    '触发运行'
  );

  const triggerCard = el(
    'section',
    { class: 'card section' },
    el('div', { class: 'card-head' }, el('h2', { text: '触发运行' })),
    el(
      'div',
      { class: 'card-body stack' },
      el('div', { class: 'tabs', role: 'tablist' }, modeSuiteButton, modeCasesButton),
      suitePane,
      casesPane,
      el(
        'div',
        { class: 'grid-2' },
        formField('环境', envSelect, { id: 'run-env' }),
        formField('备注 note', noteInput, { id: 'run-note' })
      ),
      el(
        'div',
        { class: 'grid-2' },
        formField('执行人 operator', operatorInput, { id: 'run-operator' }),
        el('div', { class: 'field' }, el('span', { class: 'field-label', text: '执行方式' }), el('div', { class: 'field-hint', text: '后台执行（background=true）：立即返回 queued，详情页每 800ms 轮询。' }))
      ),
      el('div', { class: 'row' }, triggerButton),
      el('div', {
        class: 'field-hint',
        text: 'request_id 由前端生成，24 小时内同 request_id 且参数一致会复用既有运行（幂等）。',
      })
    )
  );
  main.appendChild(triggerCard);

  /* -------------------------------------------------------- 运行列表 */

  const statusFilter = selectControl(
    [{ value: '', label: '全部状态' }].concat(statusOptions('run').map((item) => ({ value: item.value, label: item.label }))),
    {
      id: 'run-filter-status',
      on: {
        change: (event) => {
          view.status = event.target.value;
          view.offset = 0;
          loadRuns();
        },
      },
    }
  );
  const suiteFilter = selectControl([{ value: '', label: '全部套件' }], {
    id: 'run-filter-suite',
    on: {
      change: (event) => {
        view.suiteId = event.target.value;
        view.offset = 0;
        loadRuns();
      },
    },
  });

  const tableHost = el('div');
  const pagerHost = el('div', { class: 'row', style: { padding: '10px 14px' } });
  const listCard = el(
    'section',
    { class: 'card' },
    el(
      'div',
      { class: 'filter-bar' },
      formField('状态', statusFilter, { id: 'run-filter-status' }),
      formField('套件', suiteFilter, { id: 'run-filter-suite' })
    ),
    tableHost,
    pagerHost
  );
  main.appendChild(listCard);

  function renderCasePicker() {
    clear(casePickerHost);
    const keyword = view.caseSearch.toLowerCase();
    const list = view.cases.filter((item) => {
      if (!keyword) return true;
      const path = (item.request && item.request.path) || '';
      return (
        String(item.name || '').toLowerCase().indexOf(keyword) >= 0 ||
        String(path).toLowerCase().indexOf(keyword) >= 0 ||
        String(item.folder || '').toLowerCase().indexOf(keyword) >= 0
      );
    });
    if (!list.length) {
      casePickerHost.appendChild(el('li', {}, el('span', { class: 'muted', text: '没有匹配的用例' })));
      return;
    }
    list.forEach((item) => {
      const checked = view.selectedCaseIds.indexOf(item.id) >= 0;
      const box = el('input', {
        type: 'checkbox',
        id: 'run-pick-' + item.id,
        checked,
        on: {
          change: (event) => {
            const index = view.selectedCaseIds.indexOf(item.id);
            if (event.target.checked && index === -1) view.selectedCaseIds.push(item.id);
            if (!event.target.checked && index >= 0) view.selectedCaseIds.splice(index, 1);
            selectedCount.textContent = String(view.selectedCaseIds.length);
          },
        },
      });
      casePickerHost.appendChild(
        el(
          'li',
          {},
          box,
          el(
            'label',
            { class: 'picker-name', for: 'run-pick-' + item.id },
            el('span', { text: item.name || '(未命名用例)' }),
            el('span', {
              class: 'faint mono',
              text: '  ' + ((item.request && item.request.method) || 'GET') + ' ' + ((item.request && item.request.path) || '/'),
            })
          ),
          item.enabled === false ? el('span', { class: 'chip chip-skipped', text: '停用' }) : null
        )
      );
    });
  }

  function renderRuns() {
    clear(tableHost);
    if (!view.runs.length) {
      tableHost.appendChild(emptyBlock('暂无运行记录', '使用上方面板触发一次运行。'));
      renderPager();
      return;
    }
    const rows = view.runs.map((run) => {
      const total = Number(run.total) || 0;
      const passed = Number(run.passed) || 0;
      const failed = Number(run.failed) || 0;
      const errored = Number(run.errored) || 0;
      const skipped = Number(run.skipped) || 0;
      const incomplete =
        typeof run.incomplete === 'number' ? run.incomplete : Math.max(0, total - passed - failed - errored - skipped);
      const active = run.status === 'queued' || run.status === 'running';
      return html`<tr>
        <td class="mono nowrap"><a href="${'#/p/' + encodeURIComponent(pid) + '/runs/' + encodeURIComponent(run.id)}">${run.id}</a></td>
        <td>${statusChip(run.status)}</td>
        <td>${run.suite_name || (run.suite_id ? '套件 #' + run.suite_id : '自选用例')}</td>
        <td>${run.env_name || (run.env_id ? '#' + run.env_id : '—')}</td>
        <td class="nowrap">${run.trigger || 'manual'}</td>
        <td class="nowrap">${total}</td>
        <td class="nowrap">${passed}</td>
        <td class="nowrap">${failed}</td>
        <td class="nowrap">${errored}</td>
        <td class="nowrap">${incomplete}</td>
        <td class="nowrap">${skipped}</td>
        <td class="nowrap">${formatDuration(run.duration_ms)}</td>
        <td class="nowrap">${formatDateTime(run.started_at || run.finished_at)}</td>
        <td class="actions">
          <a class="btn btn-sm" href="${'#/p/' + encodeURIComponent(pid) + '/runs/' + encodeURIComponent(run.id)}">详情</a>
          <a class="btn btn-sm" href="${Api.reportUrl(pid, run.id, 'xml')}" target="_blank" rel="noopener">JUnit</a>
          <a class="btn btn-sm" href="${Api.reportUrl(pid, run.id, 'html')}" target="_blank" rel="noopener">HTML</a>
          ${
            active
              ? html`<button type="button" class="btn btn-sm btn-danger" data-action="cancel" data-id="${run.id}">取消</button>`
              : ''
          }
        </td>
      </tr>`;
    });
    const table = el('table', { class: 'data' });
    mount(
      table,
      html`<caption class="muted">状态含义：${statusLabel('passed')} / ${statusLabel('failed')} / ${statusLabel('error')} / ${statusLabel('incomplete')}（缺条件未完成，CI 视为失败）/ ${statusLabel('skipped')} / ${statusLabel('canceled')}</caption>
      <thead>
        <tr>
          <th>运行 ID</th>
          <th>状态</th>
          <th>套件</th>
          <th>环境</th>
          <th>触发</th>
          <th>总</th>
          <th>通过</th>
          <th>失败</th>
          <th>错误</th>
          <th>未完成</th>
          <th>跳过</th>
          <th>耗时</th>
          <th>开始时间</th>
          <th class="nowrap">操作</th>
        </tr>
      </thead>
      <tbody>
        ${rows}
      </tbody>`
    );
    table.addEventListener('click', async (event) => {
      const button = event.target.closest('[data-action="cancel"]');
      if (!button) return;
      const [ok] = await runAction(() => Api.cancelRun(pid, button.dataset.id), '已请求取消运行');
      if (ok) loadRuns();
    });
    tableHost.appendChild(el('div', { class: 'table-wrap' }, table));
    renderPager();
  }

  function renderPager() {
    clear(pagerHost);
    const from = view.total ? view.offset + 1 : 0;
    const to = Math.min(view.offset + view.runs.length, view.total);
    pagerHost.appendChild(el('span', { class: 'muted', text: '第 ' + from + '–' + to + ' 条 / 共 ' + view.total + ' 条' }));
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
            loadRuns();
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
            loadRuns();
          },
        },
      })
    );
  }

  async function loadRuns(silent) {
    if (!silent) {
      clear(tableHost);
      tableHost.appendChild(loadingBlock('正在加载运行记录…'));
    }
    let data;
    try {
      data = await Api.listRuns(
        pid,
        { status: view.status, suite_id: view.suiteId, limit: view.limit, offset: view.offset },
        { signal: ctx.signal }
      );
    } catch (err) {
      if (silent) return;
      clear(tableHost);
      tableHost.appendChild(errorBanner(err));
      return;
    }
    if (ctx.signal.aborted) return;
    view.runs = itemsOf(data);
    view.total = totalOf(data);
    renderRuns();
    scheduleRefresh();
  }

  /** 列表里存在 queued/running 时自动刷新，全部终态后停止。 */
  function scheduleRefresh() {
    if (view.timer) {
      clearTimeout(view.timer);
      view.timer = null;
    }
    const hasActive = view.runs.some((run) => run.status === 'queued' || run.status === 'running');
    if (!hasActive) return;
    view.timer = setTimeout(() => {
      if (ctx.signal.aborted) return;
      loadRuns(true);
    }, ACTIVE_REFRESH_MS);
  }

  async function loadSuites() {
    const [ok, data] = await runAction(() => Api.listSuites(pid, {}, { signal: ctx.signal }));
    view.suites = ok ? itemsOf(data) : [];
    view.suites.forEach((suite) => {
      suiteSelect.appendChild(el('option', { value: String(suite.id), text: suite.name || '套件 #' + suite.id }));
      suiteFilter.appendChild(el('option', { value: String(suite.id), text: suite.name || '套件 #' + suite.id }));
    });
  }

  async function loadEnvironments() {
    const [ok, data] = await runAction(() => Api.listEnvironments(pid, { signal: ctx.signal }));
    view.environments = ok ? itemsOf(data) : [];
    view.environments.forEach((env) => {
      envSelect.appendChild(
        el('option', { value: String(env.id), text: env.name + (env.is_default ? '（默认）' : '') })
      );
    });
  }

  async function loadCases() {
    const [ok, data] = await runAction(() =>
      Api.listCases(pid, { limit: 500, offset: 0 }, { signal: ctx.signal })
    );
    view.cases = ok ? itemsOf(data) : [];
    renderCasePicker();
  }

  ctx.signal.addEventListener('abort', () => {
    if (view.timer) clearTimeout(view.timer);
  });

  await loadSuites();
  await loadEnvironments();
  await loadCases();
  setMode('suite');
  await loadRuns();
}
