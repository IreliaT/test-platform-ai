/**
 * 套件：GET/POST /api/v1/projects/{pid}/suites
 * 套件编辑 + 用例选择器（按顺序写入 case_ids）+ 环境 / 失败策略 / 并发。
 */

import { Api, newRequestId } from '../api.js';
import {
  clear,
  confirmDialog,
  el,
  emptyBlock,
  errorBanner,
  formField,
  icon,
  itemsOf,
  loadingBlock,
  numberControl,
  runAction,
  selectControl,
  textControl,
  textareaControl,
  toast,
  totalOf,
} from '../app.js';

const CASE_PAGE_SIZE = 500;

function emptySuite() {
  return {
    id: null,
    name: '',
    description: '',
    case_ids: [],
    env_id: null,
    failure_strategy: 'continue',
    concurrency: 1,
    tags: [],
  };
}

export async function render(ctx) {
  const pid = ctx.pid;
  const view = {
    suites: [],
    cases: [],
    caseMap: {},
    environments: [],
    current: null,
    dirty: false,
    search: '',
  };

  const main = ctx.container;
  clear(main);
  main.appendChild(
    el(
      'div',
      { class: 'page-head' },
      el('h1', { text: '套件' }),
      el('button', {
        type: 'button',
        class: 'btn btn-primary',
        on: { click: () => selectSuite(emptySuite(), true) },
      }, icon('i-plus'), '新建套件'),
      el('div', { class: 'page-sub', text: '套件按顺序执行 case_ids；支持失败策略与并发度，可直接触发一次运行。' })
    )
  );

  const listHost = el('div', { class: 'picker' });
  const listCard = el(
    'section',
    { class: 'card' },
    el('div', { class: 'card-head' }, el('h2', { text: '套件列表' }), el('span', { class: 'pill-count', id: 'suite-count' })),
    listHost
  );
  const editorHost = el('section', { class: 'card' });
  main.appendChild(el('div', { class: 'split' }, listCard, editorHost));

  function renderList() {
    clear(listHost);
    const countNode = document.getElementById('suite-count');
    if (countNode) countNode.textContent = String(view.suites.length);
    if (!view.suites.length) {
      listHost.appendChild(emptyBlock('还没有套件', '点击「新建套件」把常用用例组织成一次可复用的运行。'));
      return;
    }
    const list = el('ul', { class: 'picker-list' });
    view.suites.forEach((suite) => {
      const current = view.current && view.current.id === suite.id;
      const button = el(
        'button',
        {
          type: 'button',
          class: 'link-btn',
          style: { textAlign: 'left', width: '100%' },
          'aria-current': current ? 'true' : null,
          on: { click: () => openSuiteFromList(suite) },
        },
        el('span', { text: suite.name || '(未命名套件)' }),
        el(
          'span',
          { class: 'row tight' },
          el('span', { class: 'chip', text: (suite.case_ids || []).length + ' 条用例' }),
          suite.failure_strategy === 'stop' ? el('span', { class: 'chip chip-incomplete', text: '失败即停' }) : null,
          suite.concurrency > 1 ? el('span', { class: 'chip', text: '并发 ' + suite.concurrency }) : null
        )
      );
      const li = el('li', {}, button);
      if (current) li.setAttribute('aria-selected', 'true');
      list.appendChild(li);
    });
    listHost.appendChild(list);
  }

  async function loadSuites() {
    clear(listHost);
    listHost.appendChild(loadingBlock('正在加载套件…'));
    try {
      const data = await Api.listSuites(pid, {}, { signal: ctx.signal });
      view.suites = itemsOf(data);
    } catch (err) {
      clear(listHost);
      listHost.appendChild(errorBanner(err));
      return;
    }
    renderList();
  }

  async function loadCases() {
    const [ok, data] = await runAction(() =>
      Api.listCases(pid, { limit: CASE_PAGE_SIZE, offset: 0 }, { signal: ctx.signal })
    );
    view.cases = ok ? itemsOf(data) : [];
    view.caseMap = {};
    view.cases.forEach((item) => {
      view.caseMap[String(item.id)] = item;
    });
    return totalOf(data);
  }

  async function loadEnvironments() {
    const [ok, data] = await runAction(() => Api.listEnvironments(pid, { signal: ctx.signal }));
    view.environments = ok ? itemsOf(data) : [];
  }

  async function openSuiteFromList(suite) {
    if (view.dirty) {
      const confirmed = await confirmDialog({
        title: '放弃未保存的修改？',
        message: '当前套件有未保存的修改，切换后会丢失。',
        confirmLabel: '放弃修改并切换',
        danger: false,
      });
      if (!confirmed) return;
    }
    let spec = suite;
    try {
      spec = await Api.getSuite(pid, suite.id, { signal: ctx.signal });
    } catch (err) {
      toast(err.message || '套件详情加载失败', 'error');
      return;
    }
    selectSuite(spec, false);
  }

  function setHashSilently(hash) {
    try {
      window.history.replaceState(null, '', hash);
    } catch (err) {
      /* 忽略 */
    }
  }

  function selectSuite(spec, isNew) {
    view.current = Object.assign({}, spec, { case_ids: (spec.case_ids || []).slice() });
    view.dirty = false;
    view.search = '';
    if (isNew) setHashSilently('#/p/' + encodeURIComponent(pid) + '/suites');
    else if (spec.id) setHashSilently('#/p/' + encodeURIComponent(pid) + '/suites/' + encodeURIComponent(spec.id));
    renderEditor();
    renderList();
  }

  function markDirty() {
    view.dirty = true;
    const badge = document.getElementById('suite-dirty');
    if (badge) badge.hidden = false;
  }

  function caseName(id) {
    const item = view.caseMap[String(id)];
    if (!item) return '用例 #' + id + '（不在当前列表）';
    return (item.name || '(未命名用例)') + '  ·  ' + ((item.request && item.request.method) || 'GET') + ' ' + ((item.request && item.request.path) || '/');
  }

  function renderEditor() {
    clear(editorHost);
    const spec = view.current;
    if (!spec) {
      editorHost.appendChild(emptyBlock('未选择套件', '从左侧选择套件，或点击「新建套件」。'));
      return;
    }
    const isNew = !spec.id;

    const nameInput = textControl({
      id: 'suite-name',
      value: spec.name || '',
      on: {
        input: (event) => {
          spec.name = event.target.value;
          markDirty();
        },
      },
    });
    const descriptionArea = textareaControl({
      id: 'suite-description',
      rows: 3,
      value: spec.description || '',
      on: {
        input: (event) => {
          spec.description = event.target.value;
          markDirty();
        },
      },
    });
    const envSelect = selectControl(
      [{ value: '', label: '不指定（运行时可覆盖）' }].concat(
        view.environments.map((env) => ({ value: String(env.id), label: env.name + (env.is_default ? '（默认）' : '') }))
      ),
      {
        id: 'suite-env',
        value: spec.env_id === null || spec.env_id === undefined ? '' : String(spec.env_id),
        on: {
          change: (event) => {
            spec.env_id = event.target.value ? Number(event.target.value) : null;
            markDirty();
          },
        },
      }
    );
    const strategySelect = selectControl(
      [
        { value: 'continue', label: 'continue：失败后继续执行' },
        { value: 'stop', label: 'stop：遇失败立即停止' },
      ],
      {
        id: 'suite-strategy',
        value: spec.failure_strategy || 'continue',
        on: {
          change: (event) => {
            spec.failure_strategy = event.target.value;
            markDirty();
          },
        },
      }
    );
    const concurrencyInput = numberControl({
      id: 'suite-concurrency',
      value: spec.concurrency || 1,
      min: 1,
      max: 32,
      on: {
        input: (event) => {
          spec.concurrency = Math.max(1, Number(event.target.value || 1));
          markDirty();
        },
      },
    });
    const tagsInput = textControl({
      id: 'suite-tags',
      value: (spec.tags || []).join(', '),
      placeholder: '多个标签用英文逗号分隔',
      on: {
        input: (event) => {
          spec.tags = event.target.value
            .split(',')
            .map((item) => item.trim())
            .filter(Boolean);
          markDirty();
        },
      },
    });

    /* -------- 用例选择器 -------- */

    const searchInput = textControl({
      id: 'suite-case-search',
      value: view.search,
      placeholder: '搜索用例名称 / 路径',
      on: {
        input: (event) => {
          view.search = event.target.value.trim();
          renderPickerList();
        },
      },
    });
    const pickerListHost = el('ul', { class: 'picker-list' });
    const pickerBox = el('div', { class: 'picker' }, pickerListHost);
    const orderHost = el('div');
    const orderCount = el('span', { class: 'pill-count' });

    function filteredCases() {
      const keyword = view.search.toLowerCase();
      if (!keyword) return view.cases;
      return view.cases.filter((item) => {
        const path = (item.request && item.request.path) || '';
        return (
          String(item.name || '').toLowerCase().indexOf(keyword) >= 0 ||
          String(path).toLowerCase().indexOf(keyword) >= 0 ||
          String(item.folder || '').toLowerCase().indexOf(keyword) >= 0
        );
      });
    }

    function toggleCase(id, checked) {
      const index = spec.case_ids.indexOf(id);
      if (checked && index === -1) spec.case_ids.push(id);
      if (!checked && index >= 0) spec.case_ids.splice(index, 1);
      markDirty();
      renderOrderList();
      renderPickerList();
    }

    function renderPickerList() {
      clear(pickerListHost);
      const list = filteredCases();
      if (!list.length) {
        pickerListHost.appendChild(el('li', {}, el('span', { class: 'muted', text: '没有匹配的用例' })));
        return;
      }
      list.forEach((item) => {
        const checked = spec.case_ids.indexOf(item.id) >= 0;
        const box = el('input', {
          type: 'checkbox',
          checked,
          id: 'pick-case-' + item.id,
          on: {
            change: (event) => toggleCase(item.id, event.target.checked),
          },
        });
        pickerListHost.appendChild(
          el(
            'li',
            {},
            box,
            el(
              'label',
              { class: 'picker-name', for: 'pick-case-' + item.id },
              el('span', { text: item.name || '(未命名用例)' }),
              el('span', { class: 'faint mono', text: '  ' + ((item.request && item.request.method) || 'GET') + ' ' + ((item.request && item.request.path) || '/') })
            ),
            el('span', { class: 'chip', text: item.priority || 'P2' }),
            item.enabled === false ? el('span', { class: 'chip chip-skipped', text: '停用' }) : null
          )
        );
      });
    }

    function moveCase(index, delta) {
      const target = index + delta;
      if (target < 0 || target >= spec.case_ids.length) return;
      const tmp = spec.case_ids[target];
      spec.case_ids[target] = spec.case_ids[index];
      spec.case_ids[index] = tmp;
      markDirty();
      renderOrderList();
    }

    function renderOrderList() {
      clear(orderHost);
      orderCount.textContent = spec.case_ids.length + ' 条';
      if (!spec.case_ids.length) {
        orderHost.appendChild(el('div', { class: 'field-hint', text: '尚未选择用例：套件至少需要一条用例才能运行。' }));
        return;
      }
      const list = el('ul', { class: 'order-list' });
      spec.case_ids.forEach((id, index) => {
        list.appendChild(
          el(
            'li',
            {},
            el('span', { class: 'picker-name', text: caseName(id) }),
            el('button', {
              type: 'button',
              class: 'btn btn-sm',
              'aria-label': '上移第 ' + (index + 1) + ' 条用例',
              disabled: index === 0,
              on: { click: () => moveCase(index, -1) },
            }, icon('i-up', 'icon-sm')),
            el('button', {
              type: 'button',
              class: 'btn btn-sm',
              'aria-label': '下移第 ' + (index + 1) + ' 条用例',
              disabled: index === spec.case_ids.length - 1,
              on: { click: () => moveCase(index, 1) },
            }, icon('i-down', 'icon-sm')),
            el('button', {
              type: 'button',
              class: 'btn btn-sm btn-danger',
              'aria-label': '移除第 ' + (index + 1) + ' 条用例',
              on: {
                click: () => {
                  spec.case_ids.splice(index, 1);
                  markDirty();
                  renderOrderList();
                  renderPickerList();
                },
              },
            }, icon('i-trash', 'icon-sm'))
          )
        );
      });
      orderHost.appendChild(list);
    }

    /* -------- 操作 -------- */

    const dirtyBadge = el('span', { class: 'chip chip-incomplete', id: 'suite-dirty', text: '未保存', hidden: !view.dirty });

    function buildPayload() {
      const name = nameInput.value.trim();
      if (!name) throw new Error('套件名称不能为空');
      return {
        name,
        description: descriptionArea.value,
        case_ids: spec.case_ids.slice(),
        env_id: spec.env_id === null || spec.env_id === undefined ? null : Number(spec.env_id),
        failure_strategy: strategySelect.value,
        concurrency: Math.max(1, Number(concurrencyInput.value || 1)),
        tags: tagsInput.value
          .split(',')
          .map((item) => item.trim())
          .filter(Boolean),
      };
    }

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
              () => (isNew ? Api.createSuite(pid, payload) : Api.updateSuite(pid, spec.id, payload)),
              isNew ? '套件已创建' : '套件已保存'
            );
            if (!ok) return;
            view.dirty = false;
            const saved = (data && (data.suite || data.suite_spec)) || data;
            await loadSuites();
            if (saved && saved.id) selectSuite(saved, false);
            else if (isNew) selectSuite(emptySuite(), true);
          },
        },
      },
      icon('i-check'),
      isNew ? '创建套件' : '保存'
    );

    const runButton = el(
      'button',
      {
        type: 'button',
        class: 'btn',
        on: {
          click: async () => {
            if (!spec.id) {
              toast('请先保存套件再运行', 'error');
              return;
            }
            if (!spec.case_ids.length) {
              toast('套件中没有用例，无法运行', 'error');
              return;
            }
            const payload = {
              suite_id: spec.id,
              trigger: 'manual',
              note: '',
              operator: '',
              request_id: newRequestId(),
              background: true,
            };
            if (spec.env_id !== null && spec.env_id !== undefined) payload.env_id = Number(spec.env_id);
            runButton.disabled = true;
            const [ok, data] = await runAction(() => Api.createRun(pid, payload), '运行已触发');
            runButton.disabled = false;
            if (!ok) return;
            const run = (data && (data.run || data.run_result)) || data || {};
            if (run && run.id) ctx.navigate('#/p/' + encodeURIComponent(pid) + '/runs/' + encodeURIComponent(run.id));
            else ctx.navigate('#/p/' + encodeURIComponent(pid) + '/runs');
          },
        },
      },
      icon('i-play'),
      '运行此套件'
    );

    editorHost.appendChild(
      el(
        'div',
        { class: 'card-head' },
        el('h2', { text: isNew ? '新建套件' : '编辑套件 ' + (spec.id ? '#' + spec.id : '') }),
        dirtyBadge,
        el('div', { class: 'spacer' }),
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
                      title: '删除套件',
                      message: '将删除套件「' + (spec.name || spec.id) + '」，用例本身不受影响。',
                      confirmLabel: '删除套件',
                    });
                    if (!confirmed) return;
                    const [ok] = await runAction(() => Api.deleteSuite(pid, spec.id), '套件已删除');
                    if (ok) {
                      view.current = null;
                      view.dirty = false;
                      setHashSilently('#/p/' + encodeURIComponent(pid) + '/suites');
                      await loadSuites();
                      renderEditor();
                    }
                  },
                },
              },
              icon('i-trash', 'icon-sm'),
              '删除'
            ),
        isNew ? null : runButton,
        saveButton
      )
    );

    editorHost.appendChild(
      el(
        'div',
        { class: 'card-body stack' },
        el(
          'div',
          { class: 'grid-2' },
          formField('套件名称 *', nameInput, { id: 'suite-name' }),
          formField('标签 tags', tagsInput, { id: 'suite-tags' })
        ),
        formField('描述 description', descriptionArea, { id: 'suite-description' }),
        el(
          'div',
          { class: 'grid-3' },
          formField('环境 env_id', envSelect, { id: 'suite-env' }),
          formField('失败策略 failure_strategy', strategySelect, { id: 'suite-strategy' }),
          formField('并发 concurrency', concurrencyInput, { id: 'suite-concurrency', hint: '1 表示串行执行' })
        ),
        el(
          'fieldset',
          { class: 'subform' },
          el('legend', {}, '用例选择（case_ids 顺序即执行顺序） ', orderCount),
          el(
            'div',
            { class: 'stack' },
            formField('搜索用例', searchInput, { id: 'suite-case-search', hint: '共 ' + view.cases.length + ' 条候选用例' }),
            pickerBox,
            el('div', { class: 'subform-title' }, '已选用例（按执行顺序）'),
            orderHost
          )
        )
      )
    );

    renderPickerList();
    renderOrderList();
  }

  await loadEnvironments();
  await loadCases();
  await loadSuites();
  renderEditor();
}
