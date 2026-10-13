/**
 * 项目概览：GET /api/v1/projects/{pid}/stats
 * 统计卡片 + 最近运行表格 + 纯 SVG 趋势图（无图表库）。
 */

import { Api } from '../api.js';
import {
  clear,
  el,
  errorBanner,
  escapeHtml,
  formatDateTime,
  formatDuration,
  formatPassRate,
  icon,
  itemsOf,
  loadingBlock,
  mount,
  raw,
  routeHash,
  html,
  statusChip,
  statusLabel,
} from '../app.js';

const COUNT_CARDS = [
  { key: 'endpoint', label: '接口', view: 'endpoints' },
  { key: 'test_case', label: '用例', view: 'cases' },
  { key: 'suite', label: '套件', view: 'suites' },
  { key: 'environment', label: '环境', view: 'environments' },
  { key: 'run', label: '运行', view: 'runs' },
];

const TREND_SERIES = [
  { key: 'passed', label: '通过', cls: 'bar-passed' },
  { key: 'failed', label: '失败', cls: 'bar-failed' },
  { key: 'errored', label: '错误', cls: 'bar-errored' },
  { key: 'incomplete', label: '未完成', cls: 'bar-incomplete' },
  { key: 'skipped', label: '跳过', cls: 'bar-skipped' },
];

function countOf(counts, key) {
  const value = counts && counts[key];
  return typeof value === 'number' ? value : 0;
}

/** 趋势点：兼容 trend 数组里出现 passed/failed 或 total 的不同写法。 */
function trendPoint(point, index) {
  const item = point || {};
  const values = {};
  let sum = 0;
  TREND_SERIES.forEach((series) => {
    const raw = Number(item[series.key]);
    values[series.key] = Number.isFinite(raw) && raw > 0 ? raw : 0;
    sum += values[series.key];
  });
  if (!sum && Number.isFinite(Number(item.total))) {
    values.passed = Number(item.total) || 0;
    sum = values.passed;
  }
  return {
    label: item.date || item.day || item.label || '#' + (index + 1),
    values,
    total: sum,
    rate: sum ? values.passed / sum : 0,
  };
}

function buildTrendChart(points) {
  const width = 760;
  const height = 190;
  const padLeft = 34;
  const padRight = 14;
  const padTop = 14;
  const padBottom = 26;
  const plotWidth = width - padLeft - padRight;
  const plotHeight = height - padTop - padBottom;
  const maxTotal = Math.max.apply(
    null,
    points.map((point) => point.total).concat([1])
  );
  const step = plotWidth / points.length;
  const barWidth = Math.max(6, Math.min(26, step * 0.58));

  const parts = [];
  parts.push(
    '<line class="axis" x1="' + padLeft + '" y1="' + (padTop + plotHeight) + '" x2="' + (width - padRight) +
      '" y2="' + (padTop + plotHeight) + '" />'
  );
  parts.push('<line class="axis" x1="' + padLeft + '" y1="' + padTop + '" x2="' + padLeft + '" y2="' + (padTop + plotHeight) + '" />');
  parts.push(
    '<text class="axis-label" x="4" y="' + (padTop + 8) + '">' + maxTotal + '</text>' +
      '<text class="axis-label" x="4" y="' + (padTop + plotHeight) + '">0</text>'
  );

  const linePoints = [];
  points.forEach((point, index) => {
    const center = padLeft + step * index + step / 2;
    let cursor = padTop + plotHeight;
    const labelParts = [point.label, '共 ' + point.total];
    TREND_SERIES.forEach((series) => {
      const value = point.values[series.key];
      if (!value) return;
      const barHeight = (value / maxTotal) * plotHeight;
      cursor -= barHeight;
      labelParts.push(series.label + ' ' + value);
      parts.push(
        '<rect class="' + series.cls + '" x="' + (center - barWidth / 2).toFixed(1) + '" y="' + cursor.toFixed(1) +
          '" width="' + barWidth.toFixed(1) + '" height="' + barHeight.toFixed(1) + '" rx="2">' +
          '<title>' + escapeHtml(labelParts.join('，')) + '</title></rect>'
      );
    });
    linePoints.push((center).toFixed(1) + ',' + (padTop + plotHeight - point.rate * plotHeight).toFixed(1));
    if (index === 0 || index === points.length - 1 || points.length <= 8 || index % Math.ceil(points.length / 8) === 0) {
      parts.push(
        '<text class="axis-label" x="' + center.toFixed(1) + '" y="' + (height - 8) + '" text-anchor="middle">' +
          escapeHtml(String(point.label).slice(5)) + '</text>'
      );
    }
  });

  if (linePoints.length > 1) {
    parts.push('<polyline class="trend-line" points="' + linePoints.join(' ') + '" />');
  }
  points.forEach((point, index) => {
    const center = padLeft + step * index + step / 2;
    parts.push(
      '<circle class="trend-dot" cx="' + center.toFixed(1) + '" cy="' +
        (padTop + plotHeight - point.rate * plotHeight).toFixed(1) + '" r="2.5" />'
    );
  });

  const summary = points
    .map((point) => point.label + ' 通过率 ' + formatPassRate(point.rate) + '，共 ' + point.total)
    .join('；');
  return html`<svg
    class="chart"
    viewBox="0 0 ${width} ${height}"
    role="img"
    aria-label="最近 ${points.length} 次运行趋势：${summary}"
  >
    ${raw(parts.join(''))}
  </svg>`;
}

function renderRunsTable(pid, runs) {
  if (!runs.length) {
    return html`<div class="empty">
      <div class="empty-title">暂无运行记录</div>
      <div>到「运行与历史」触发一次运行后，这里会显示最近结果。</div>
    </div>`;
  }
  const rows = runs.map((run) => {
    const total = Number(run.total) || 0;
    const passed = Number(run.passed) || 0;
    const failed = Number(run.failed) || 0;
    const errored = Number(run.errored) || 0;
    const incomplete =
      typeof run.incomplete === 'number'
        ? run.incomplete
        : Math.max(0, total - passed - failed - errored - (Number(run.skipped) || 0));
    return html`<tr>
      <td class="mono"><a href="${routeHash(pid, 'runs', run.id)}">${run.id}</a></td>
      <td>${statusChip(run.status)}</td>
      <td>${run.suite_name || (run.suite_id ? '套件 #' + run.suite_id : '自选用例')}</td>
      <td class="nowrap">${total}</td>
      <td class="nowrap">${passed}</td>
      <td class="nowrap">${failed}</td>
      <td class="nowrap">${errored}</td>
      <td class="nowrap">${incomplete}</td>
      <td class="nowrap">${formatDuration(run.duration_ms)}</td>
      <td class="nowrap">${formatDateTime(run.started_at || run.finished_at)}</td>
    </tr>`;
  });
  return html`<div class="table-wrap">
    <table class="data">
      <caption class="muted">
        最近 ${runs.length} 次运行（状态含义：${statusLabel('passed')} / ${statusLabel('failed')} /
        ${statusLabel('error')} / ${statusLabel('incomplete')} / ${statusLabel('skipped')}）
      </caption>
      <thead>
        <tr>
          <th>运行 ID</th>
          <th>状态</th>
          <th>套件</th>
          <th>总数</th>
          <th>通过</th>
          <th>失败</th>
          <th>错误</th>
          <th>未完成</th>
          <th>耗时</th>
          <th>开始时间</th>
        </tr>
      </thead>
      <tbody>
        ${rows}
      </tbody>
    </table>
  </div>`;
}

export async function render(ctx) {
  const main = ctx.container;
  clear(main);
  main.appendChild(loadingBlock('正在加载项目概览…'));

  let stats;
  try {
    stats = await Api.projectStats(ctx.pid, { signal: ctx.signal });
  } catch (err) {
    clear(main);
    main.appendChild(errorBanner(err));
    return;
  }
  if (ctx.signal.aborted) return;

  const data = stats || {};
  const counts = data.counts || {};
  const recentRuns = itemsOf(data.recent_runs);
  const trend = (Array.isArray(data.trend) ? data.trend : []).map(trendPoint);

  clear(main);
  main.appendChild(
    el(
      'div',
      { class: 'page-head' },
      el('h1', { text: '项目概览' }),
      el('a', { class: 'btn btn-sm', href: routeHash(ctx.pid, 'imports') }, icon('i-import'), '导入接口'),
      el('a', { class: 'btn btn-sm', href: routeHash(ctx.pid, 'runs') }, icon('i-play'), '触发运行'),
      el('a', {
        class: 'btn btn-sm',
        href: Api.exportUrl(ctx.pid),
        target: '_blank',
        rel: 'noopener',
      }, icon('i-import'), '导出全部数据'),
      el(
        'div',
        { class: 'page-sub' },
        '项目 ',
        el('span', { class: 'mono', text: ctx.pid }),
        ' · 接口目录、用例、运行历史与报告都按项目隔离'
      )
    )
  );

  const statGrid = el('div', { class: 'stat-grid section' });
  COUNT_CARDS.forEach((card) => {
    statGrid.appendChild(
      el(
        'a',
        { class: 'stat', href: routeHash(ctx.pid, card.view) },
        el('div', { class: 'stat-label', text: card.label }),
        el('div', { class: 'stat-value', text: String(countOf(counts, card.key)) })
      )
    );
  });
  statGrid.appendChild(
    el(
      'div',
      { class: 'stat' },
      el('div', { class: 'stat-label', text: '通过率' }),
      el('div', { class: 'stat-value', text: formatPassRate(data.pass_rate) }),
      el('div', {
        class: 'stat-hint',
        text: '最近运行：' + (data.last_run ? data.last_run.id + '（' + statusLabel(data.last_run.status) + '）' : '暂无'),
      })
    )
  );
  main.appendChild(statGrid);

  const extra = el(
    'div',
    { class: 'row muted section' },
    el('span', { text: '用例结果记录 ' + countOf(counts, 'run_case_result') + ' 条' }),
    el('span', { text: '·' }),
    el('span', { text: '导入批次 ' + countOf(counts, 'import_batch') + ' 个' })
  );
  main.appendChild(extra);

  const trendCard = el(
    'section',
    { class: 'card section' },
    el(
      'div',
      { class: 'card-head' },
      el('h2', { text: '运行趋势' }),
      el('span', { class: 'muted', text: '柱状：各状态用例数；折线：通过率' })
    ),
    el(
      'div',
      { class: 'card-body' },
      trend.length ? mount(el('div'), buildTrendChart(trend)) : el('div', { class: 'chart-empty', text: '暂无趋势数据' }),
      el(
        'div',
        { class: 'chart-legend' },
        TREND_SERIES.map((series) =>
          el(
            'span',
            { class: 'legend-item' },
            el('span', { class: 'legend-swatch', style: { background: 'var(--' + swatchVar(series.key) + ')' } }),
            series.label
          )
        ),
        el(
          'span',
          { class: 'legend-item' },
          el('span', { class: 'legend-swatch', style: { background: 'var(--accent)' } }),
          '通过率'
        )
      )
    )
  );
  main.appendChild(trendCard);

  main.appendChild(
    el(
      'section',
      { class: 'card section' },
      el(
        'div',
        { class: 'card-head' },
        el('h2', { text: '最近运行' }),
        el('a', { class: 'btn btn-sm', href: routeHash(ctx.pid, 'runs') }, '查看全部')
      ),
      mount(el('div'), renderRunsTable(ctx.pid, recentRuns))
    )
  );
}

function swatchVar(key) {
  if (key === 'passed') return 'ok';
  if (key === 'failed') return 'fail';
  if (key === 'errored') return 'fail';
  if (key === 'incomplete') return 'warn';
  return 'skip';
}
