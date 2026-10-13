/**
 * 运行详情：GET /api/v1/projects/{pid}/runs/{run_id}
 * queued/running 时每 800ms 轮询；终态停止。
 * 另含取消（POST …/cancel）、报告下载（…/report?format=xml|html|json）与基线对比（…/diff?base=）。
 */

import { Api, RUN_POLL_INTERVAL_MS, isRunActive, poll } from '../api.js';
import {
  caseResultBlock,
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
  toast,
} from '../app.js';

const DIFF_KIND_LABELS = {
  added: '新增用例',
  removed: '移除用例',
  status_changed: '状态变化',
  assertion_changed: '断言变化',
};

function diffKindChip(kind) {
  const label = DIFF_KIND_LABELS[kind] || kind;
  const cls =
    kind === 'added' ? 'chip-passed' : kind === 'removed' ? 'chip-failed' : kind === 'status_changed' ? 'chip-incomplete' : 'chip-queued';
  return html`<span class="chip ${cls}">${label}</span>`;
}

function normalizeRun(data) {
  if (!data) return null;
  if (data.run && typeof data.run === 'object') return data.run;
  if (data.run_result && typeof data.run_result === 'object') return data.run_result;
  return data;
}

function counterGrid(run) {
  const total = Number(run.total) || (Array.isArray(run.case_results) ? run.case_results.length : 0);
  const passed = Number(run.passed) || 0;
  const failed = Number(run.failed) || 0;
  const errored = Number(run.errored) || 0;
  const skipped = Number(run.skipped) || 0;
  const incomplete =
    typeof run.incomplete === 'number' ? run.incomplete : Math.max(0, total - passed - failed - errored - skipped);
  const items = [
    { label: '总用例', value: total },
    { label: '通过 passed', value: passed },
    { label: '失败 failed', value: failed },
    { label: '错误 error', value: errored },
    { label: '未完成 incomplete', value: incomplete },
    { label: '跳过 skipped', value: skipped },
    { label: '耗时', value: formatDuration(run.duration_ms) },
  ];
  return el(
    'div',
    { class: 'stat-grid' },
    items.map((item) =>
      el(
        'div',
        { class: 'stat' },
        el('div', { class: 'stat-label', text: item.label }),
        el('div', { class: 'stat-value', text: String(item.value) })
      )
    )
  );
}

export async function render(ctx) {
  const pid = ctx.pid;
  const runId = ctx.arg;
  const main = ctx.container;
  const view = {
    run: null,
    openCases: {},
    stop: null,
    otherRuns: [],
    diff: null,
    diffBase: '',
  };

  clear(main);
  if (!runId) {
    main.appendChild(emptyBlock('缺少运行 ID', '请从运行列表进入详情。'));
    return;
  }

  const bannerHost = el('div');
  const bodyHost = el('div');
  const loadingNode = loadingBlock('正在加载运行结果…');
  main.appendChild(bannerHost);
  main.appendChild(loadingNode);
  main.appendChild(bodyHost);

  function hideLoading() {
    if (loadingNode.parentNode) loadingNode.parentNode.removeChild(loadingNode);
  }

  function renderRun(run) {
    hideLoading();
    view.run = run;
    clear(bodyHost);
    const active = isRunActive(run);

    bodyHost.appendChild(
      el(
        'div',
        { class: 'card section' },
        el(
          'div',
          { class: 'card-head' },
          el('h2', {}, '运行 ', el('span', { class: 'mono', text: run.id })),
          statusChip(run.status),
          active
            ? el(
                'span',
                { class: 'loading-indicator' },
                el('span', { class: 'spinner', 'aria-hidden': 'true' }),
                '每 ' + RUN_POLL_INTERVAL_MS + 'ms 轮询中…'
              )
            : el('span', { class: 'faint', text: '终态，已停止轮询' }),
          el('div', { class: 'spacer' }),
          el('a', { class: 'btn btn-sm', href: '#/p/' + encodeURIComponent(pid) + '/runs' }, '返回列表'),
          el('button', {
            type: 'button',
            class: 'btn btn-sm',
            disabled: !active,
            on: {
              click: async () => {
                const [ok] = await runAction(() => Api.cancelRun(pid, run.id), '已请求取消运行');
                if (ok) refresh();
              },
            },
          }, icon('i-cancel', 'icon-sm'), '取消运行'),
          el('a', { class: 'btn btn-sm', href: Api.reportUrl(pid, run.id, 'xml'), target: '_blank', rel: 'noopener' }, '下载 JUnit XML'),
          el('a', { class: 'btn btn-sm', href: Api.reportUrl(pid, run.id, 'html'), target: '_blank', rel: 'noopener' }, '下载 HTML 报告'),
          el('a', { class: 'btn btn-sm', href: Api.reportUrl(pid, run.id, 'json'), target: '_blank', rel: 'noopener' }, 'JSON')
        ),
        el(
          'div',
          { class: 'card-body stack' },
          counterGrid(run),
          el(
            'dl',
            { class: 'kv-list' },
            el('dt', { text: '套件' }),
            el('dd', { text: run.suite_name || (run.suite_id ? '#' + run.suite_id : '自选用例') }),
            el('dt', { text: '环境' }),
            el('dd', { text: run.env_name || (run.env_id ? '#' + run.env_id : '—') }),
            el('dt', { text: '触发' }),
            el('dd', { text: run.trigger || 'manual' }),
            el('dt', { text: '执行人' }),
            el('dd', { text: run.operator || '—' }),
            el('dt', { text: '备注' }),
            el('dd', { text: run.note || '—' }),
            el('dt', { text: '开始 / 结束' }),
            el('dd', { text: formatDateTime(run.started_at) + ' → ' + formatDateTime(run.finished_at) }),
            el('dt', { text: 'request_id' }),
            el('dd', { class: 'mono break-all', text: run.request_id || '—' })
          )
        )
      )
    );

    const caseResults = Array.isArray(run.case_results) ? run.case_results : [];
    const listCard = el(
      'section',
      { class: 'card section' },
      el(
        'div',
        { class: 'card-head' },
        el('h2', { text: '用例结果' }),
        el('span', { class: 'pill-count', text: String(caseResults.length) })
      ),
      el('div', { class: 'card-body' }, caseResults.length ? renderCases(caseResults) : emptyBlock('没有用例结果', active ? '运行尚未产生结果。' : '该运行没有执行任何用例。'))
    );
    bodyHost.appendChild(listCard);

    if (Array.isArray(run.logs) && run.logs.length) {
      bodyHost.appendChild(
        el(
          'section',
          { class: 'card section' },
          el('div', { class: 'card-head' }, el('h2', { text: '运行日志' })),
          el('div', { class: 'card-body' }, el('pre', { text: run.logs.join('\n') }))
        )
      );
    }

    bodyHost.appendChild(renderCompareCard(run));
  }

  function renderCases(caseResults) {
    const host = el('div');
    caseResults.forEach((result, index) => {
      const key = String(index);
      const open = Boolean(view.openCases[key]);
      const bodyId = 'case-result-' + index;
      const head = el(
        'button',
        {
          type: 'button',
          class: 'accordion-head',
          'aria-expanded': open ? 'true' : 'false',
          'aria-controls': bodyId,
        },
        icon('i-chevron', 'icon-chevron'),
        statusChip(result.status),
        el('span', { class: 'acc-title', text: result.case_name || '用例 #' + (result.case_id || index + 1) }),
        el('span', { class: 'faint mono nowrap', text: result.case_id ? '#' + result.case_id : '' }),
        el('span', { class: 'muted nowrap', text: '耗时 ' + formatDuration(result.duration_ms) }),
        result.attempts ? el('span', { class: 'faint nowrap', text: '尝试 ' + result.attempts + ' 次' }) : null,
        result.assertions && result.assertions.length
          ? el('span', {
              class: 'faint nowrap',
              text:
                '断言 ' +
                result.assertions.filter((item) => item.passed).length +
                '/' +
                result.assertions.length,
            })
          : null
      );
      const body = el('div', { class: 'accordion-body', id: bodyId, hidden: !open });
      if (open) body.appendChild(caseResultBlock(result, { showName: false }));
      head.addEventListener('click', () => {
        const next = !view.openCases[key];
        view.openCases[key] = next;
        head.setAttribute('aria-expanded', next ? 'true' : 'false');
        body.hidden = !next;
        clear(body);
        if (next) body.appendChild(caseResultBlock(result, { showName: false }));
      });
      host.appendChild(el('div', { class: 'accordion-item' }, head, body));
    });
    return host;
  }

  function renderCompareCard(run) {
    const card = el(
      'section',
      { class: 'card section' },
      el(
        'div',
        { class: 'card-head' },
        el('h2', { text: '与基线对比' }),
        el('span', { class: 'muted', text: '选择一次历史运行作为基线，查看新增失败与断言变化' })
      )
    );
    const select = selectControl(
      [{ value: '', label: '选择基线运行…' }].concat(
        view.otherRuns.map((item) => ({
          value: item.id,
          label: item.id + '（' + statusLabel(item.status) + ' · ' + formatDateTime(item.started_at || item.finished_at) + '）',
        }))
      ),
      { id: 'diff-base', value: view.diffBase }
    );
    const resultHost = el('div', { class: 'stack' });
    const compareButton = el(
      'button',
      {
        type: 'button',
        class: 'btn',
        on: {
          click: async () => {
            const base = select.value;
            if (!base) {
              toast('请选择基线运行', 'error');
              return;
            }
            if (base === run.id) {
              toast('基线与当前运行不能相同', 'error');
              return;
            }
            view.diffBase = base;
            clear(resultHost);
            resultHost.appendChild(loadingBlock('正在对比…'));
            const [ok, data] = await runAction(() => Api.diffRun(pid, run.id, base, { signal: ctx.signal }));
            clear(resultHost);
            if (!ok) return;
            view.diff = data;
            renderDiff(resultHost, data);
          },
        },
      },
      icon('i-diff'),
      '开始对比'
    );
    card.appendChild(
      el(
        'div',
        { class: 'card-body stack' },
        el(
          'div',
          { class: 'row' },
          el('div', { style: { minWidth: '280px' } }, formField('基线运行 base', select, { id: 'diff-base' })),
          compareButton
        ),
        resultHost
      )
    );
    if (view.diff && view.diffBase) renderDiff(resultHost, view.diff);
    return card;
  }

  function renderDiff(host, diff) {
    clear(host);
    const summary = (diff && diff.summary) || {};
    host.appendChild(
      el(
        'div',
        { class: 'row' },
        el('span', { class: 'chip', text: '用例 ' + (summary.cases === undefined ? '—' : summary.cases) }),
        el('span', { class: 'chip chip-incomplete', text: '变化 ' + (summary.changed === undefined ? '—' : summary.changed) }),
        el('span', { class: 'chip chip-passed', text: '新增 ' + (summary.added === undefined ? '—' : summary.added) }),
        el('span', { class: 'chip chip-failed', text: '移除 ' + (summary.removed === undefined ? '—' : summary.removed) }),
        el('span', { class: 'chip chip-failed', text: '新失败 ' + (summary.newly_failed === undefined ? '—' : summary.newly_failed) }),
        el('span', { class: 'chip chip-passed', text: '新通过 ' + (summary.newly_passed === undefined ? '—' : summary.newly_passed) }),
        el('span', { class: 'chip chip-queued', text: '状态变化 ' + (summary.status_changed === undefined ? '—' : summary.status_changed) }),
        diff && diff.same_fingerprint
          ? el('span', { class: 'chip chip-passed', text: '指纹一致 same_fingerprint' })
          : el('span', { class: 'chip chip-skipped', text: '指纹不同' })
      )
    );
    const changes = (diff && diff.changes) || [];
    if (!changes.length) {
      host.appendChild(el('div', { class: 'banner banner-ok' }, icon('i-check'), el('span', { text: '与基线相比没有差异。' })));
      return;
    }
    const rows = changes.map((change) => {
      const before = change.before || {};
      const after = change.after || {};
      return html`<tr>
        <td>${diffKindChip(change.kind)}</td>
        <td>
          <div>${change.case_name || '用例 #' + (change.case_id === undefined ? '?' : change.case_id)}</div>
          <div class="faint mono">${change.case_id === undefined || change.case_id === null ? '' : '#' + change.case_id}</div>
        </td>
        <td>${before.status ? statusChip(before.status) : html`<span class="faint">—</span>`}</td>
        <td>${after.status ? statusChip(after.status) : html`<span class="faint">—</span>`}</td>
        <td>
          ${(change.assertions || []).map(
            (item) => html`<div class="mono">
              ${item.describe || ''}
              <span class="${item.before === 'passed' ? 'diff-added' : ''}">${item.before || '—'}</span>
              →
              <span class="${item.after === 'passed' ? 'diff-added' : 'diff-removed'}">${item.after || '—'}</span>
            </div>`
          )}
        </td>
      </tr>`;
    });
    const table = el('table', { class: 'data' });
    mount(
      table,
      html`<caption class="muted">基线 ${diff.base_run_id || view.diffBase} → 当前 ${diff.run_id || runId}</caption>
      <thead>
        <tr>
          <th>类型</th>
          <th>用例</th>
          <th>基线状态</th>
          <th>当前状态</th>
          <th>断言差异</th>
        </tr>
      </thead>
      <tbody>
        ${rows}
      </tbody>`
    );
    host.appendChild(el('div', { class: 'table-wrap' }, table));
  }

  async function refresh() {
    clear(bannerHost);
    try {
      const run = normalizeRun(await Api.getRun(pid, runId, { signal: ctx.signal }));
      renderRun(run);
    } catch (err) {
      if (err && err.name === 'AbortError') return;
      clear(bodyHost);
      bodyHost.appendChild(errorBanner(err));
    }
  }

  async function loadOtherRuns() {
    const [ok, data] = await runAction(() =>
      Api.listRuns(pid, { limit: 20, offset: 0 }, { signal: ctx.signal })
    );
    view.otherRuns = ok ? itemsOf(data).filter((item) => item.id !== runId) : [];
  }

  await loadOtherRuns();

  view.stop = poll(() => Api.getRun(pid, runId, { signal: ctx.signal }).then(normalizeRun), {
    intervalMs: RUN_POLL_INTERVAL_MS,
    signal: ctx.signal,
    isDone: (run) => !isRunActive(run),
    onData: (run) => {
      clear(bannerHost);
      const wasActive = view.run ? isRunActive(view.run) : true;
      renderRun(run);
      if (wasActive && !isRunActive(run)) {
        toast('运行已结束：' + statusLabel(run.status), run.status === 'passed' ? 'success' : 'error', 5000);
        loadOtherRuns().then(() => renderRun(run));
      }
    },
    onError: (err) => {
      clear(bodyHost);
      bodyHost.appendChild(errorBanner(err));
    },
  });

  ctx.signal.addEventListener('abort', () => {
    if (view.stop) view.stop();
  });
}
