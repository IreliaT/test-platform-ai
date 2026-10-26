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
import { workflowParameters } from '../workflow.js';

const CASE_PAGE_SIZE = 500;

function emptySuite() {
  return {
    id: null,
    name: '',
    folder: '',
    description: '',
    case_ids: [],
    env_id: null,
    variables: {},
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
    folder: '',
    folders: [],
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

  const folderFilter = selectControl(
    [
      { value: '', label: '全部目录' },
      { value: '__none__', label: '未分组' },
    ],
    {
      id: 'suite-filter-folder',
      on: {
        change: (event) => {
          view.folder = event.target.value;
          renderList();
        },
      },
    }
  );
  const listHost = el('div', { class: 'picker suite-picker' });
  const listCard = el(
    'section',
    { class: 'card' },
    el('div', { class: 'card-head' }, el('h2', { text: '套件列表' }), el('span', { class: 'pill-count', id: 'suite-count' })),
    el('div', { class: 'filter-bar' }, formField('目录', folderFilter, { id: 'suite-filter-folder' }), el('div', { class: 'spacer' })),
    listHost
  );
  const editorHost = el('section', { class: 'card' });
  main.appendChild(el('div', { class: 'split' }, listCard, editorHost));

  function matchesFolder(folder) {
    if (!view.folder) return true;
    if (view.folder === '__none__') return !folder;
    return folder === view.folder;
  }

  function groupSuites() {
    const groups = new Map();
    view.suites.forEach((suite) => {
      const name = String(suite.folder || '').trim();
      if (!matchesFolder(name)) return;
      if (!groups.has(name)) groups.set(name, []);
      groups.get(name).push(suite);
    });
    // 有目录的按名称排在前面，「未分组」永远收在最后
    return Array.from(groups.entries()).sort((a, b) => {
      if (!a[0]) return 1;
      if (!b[0]) return -1;
      return a[0].localeCompare(b[0], 'zh-Hans-CN');
    });
  }

  function folderNames() {
    const names = new Set(view.folders || []);
    view.suites.forEach((suite) => {
      const name = String(suite.folder || '').trim();
      if (name) names.add(name);
    });
    return Array.from(names).sort((a, b) => a.localeCompare(b, 'zh-Hans-CN'));
  }

  async function loadFolders() {
    try {
      const data = await Api.listFolders(pid, { signal: ctx.signal });
      const payload = (data && data.folders) || data || {};
      view.folders = Array.isArray(payload.suite_folders) ? payload.suite_folders.slice() : [];
    } catch (err) {
      view.folders = [];
    }
    clear(folderFilter);
    folderFilter.appendChild(el('option', { value: '', text: '全部目录' }));
    folderFilter.appendChild(el('option', { value: '__none__', text: '未分组' }));
    view.folders.forEach((name) => folderFilter.appendChild(el('option', { value: name, text: name })));
    folderFilter.value = view.folder;
  }

  function renderList() {
    clear(listHost);
    const countNode = document.getElementById('suite-count');
    if (countNode) countNode.textContent = String(view.suites.length);
    if (!view.suites.length) {
      listHost.appendChild(emptyBlock('还没有套件', '点击「新建套件」把常用用例组织成一次可复用的运行。'));
      return;
    }
    if (!groupSuites().length) {
      listHost.appendChild(emptyBlock('该目录下没有套件', '切换到「全部目录」，或给套件设置这个目录。'));
      return;
    }
    groupSuites().forEach(([folder, suites]) => {
      const group = el('section', { class: 'suite-group' });
      group.appendChild(
        el(
          'div',
          { class: 'suite-group-head' },
          el('span', { class: 'suite-group-name', text: folder || '未分组' }),
          el('span', { class: 'pill-count', text: String(suites.length) })
        )
      );
      const list = el('ul', { class: 'picker-list' });
      suites.forEach((suite) => {
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
            suite.folder ? el('span', { class: 'chip chip-queued', text: '目录 ' + suite.folder }) : null,
            suite.failure_strategy === 'stop' ? el('span', { class: 'chip chip-incomplete', text: '失败即停' }) : null,
            suite.concurrency > 1 ? el('span', { class: 'chip', text: '并发 ' + suite.concurrency }) : null
          )
        );
        const li = el('li', {}, button);
        if (current) li.setAttribute('aria-selected', 'true');
        list.appendChild(li);
      });
      group.appendChild(list);
      listHost.appendChild(group);
    });
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
    // 目录从已有目录里选；没有合适的再「新建目录…」手填
    const knownFolders = folderNames();
    const isCustomFolder = Boolean(spec.folder) && !knownFolders.includes(spec.folder);
    const folderCustomInput = textControl({
      id: 'suite-folder-custom',
      value: isCustomFolder ? spec.folder : '',
      placeholder: '输入新目录名，例如：官方靓号库/回归',
      on: {
        input: (event) => {
          spec.folder = event.target.value;
          markDirty();
        },
      },
    });
    folderCustomInput.hidden = !isCustomFolder;
    const folderSelect = selectControl(
      [{ value: '', label: '未分组' }]
        .concat(knownFolders.map((name) => ({ value: name, label: name })))
        .concat([{ value: '__new__', label: '＋ 新建目录…' }]),
      {
        id: 'suite-folder',
        value: isCustomFolder ? '__new__' : (knownFolders.includes(spec.folder) ? spec.folder : ''),
        on: {
          change: (event) => {
            const value = event.target.value;
            if (value === '__new__') {
              folderCustomInput.hidden = false;
              folderCustomInput.focus();
              spec.folder = folderCustomInput.value.trim();
            } else {
              folderCustomInput.hidden = true;
              spec.folder = value;
            }
            markDirty();
          },
        },
      }
    );
    const folderField = el(
      'div',
      { class: 'suite-folder-field' },
      formField('目录 folder', folderSelect, { id: 'suite-folder', hint: '选已有目录，或「新建目录…」' }),
      folderCustomInput
    );
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
    const suiteVarsHost = el('div', { class: 'stack' });

    // 已选用例引用到的 ${变量}：在套件里直接填，运行时优先于环境变量
    function renderSuiteVariables() {
      clear(suiteVarsHost);
      const seen = new Map();
      spec.case_ids.forEach((id) => {
        const item = view.caseMap[String(id)];
        if (!item) return;
        workflowParameters(item).forEach((param) => {
          if (!seen.has(param.name)) seen.set(param.name, { param, cases: [] });
          seen.get(param.name).cases.push(item.name || '#' + id);
        });
      });
      if (!seen.size) {
        suiteVarsHost.appendChild(
          el('div', { class: 'field-hint', text: '所选用例没有引用 ${变量}，不需要额外参数。' })
        );
        return;
      }
      suiteVarsHost.appendChild(
        el('div', { class: 'field-hint', text: '这里填的值只在本套件运行时生效，优先于环境变量；留空则使用环境里的同名变量。' })
      );
      const grid = el('div', { class: 'grid-2' });
      seen.forEach(({ param, cases }, name) => {
        const value = spec.variables && spec.variables[name] !== undefined ? spec.variables[name] : '';
        const input = textControl({
          id: 'suite-var-' + name,
          value: String(value),
          type: param.secret ? 'password' : 'text',
          placeholder: param.secret ? '留空使用环境变量' : '',
          on: {
            input: (event) => {
              if (!spec.variables) spec.variables = {};
              spec.variables[name] = event.target.value;
              markDirty();
            },
          },
        });
        const field = formField(name + (param.required ? ' *' : '（选填）'), input, { id: 'suite-var-' + name });
        field.appendChild(el('span', { class: 'field-hint', text: '用于：' + cases.join('、') }));
        grid.appendChild(field);
      });
      suiteVarsHost.appendChild(grid);
    }

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
      // 勾选状态浏览器已经更新，这里只刷新「已选用例」，不重建候选列表——
      // 重建会把滚动位置带回第一行，勾到一半就得重新找位置。
      renderOrderList();
    }

    function renderPickerList(preserveScroll = false) {
      // 只在「删除已选用例」这类列表结构不变的操作里保留滚动位置；搜索时回到顶部
      const scrollTop = preserveScroll ? pickerBox.scrollTop : 0;
      clear(pickerListHost);
      const list = filteredCases();
      if (!list.length) {
        pickerListHost.appendChild(el('li', {}, el('span', { class: 'muted', text: '没有匹配的用例' })));
        pickerBox.scrollTop = scrollTop;
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
      pickerBox.scrollTop = scrollTop;
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
      renderSuiteVariables();
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
                  renderPickerList(true);
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
        folder: folderSelect.value === '__new__' ? folderCustomInput.value.trim() : folderSelect.value,
        description: descriptionArea.value,
        case_ids: spec.case_ids.slice(),
        env_id: spec.env_id === null || spec.env_id === undefined ? null : Number(spec.env_id),
        variables: Object.assign({}, spec.variables || {}),
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
          folderField
        ),
        el(
          'div',
          { class: 'grid-2' },
          formField('标签 tags', tagsInput, { id: 'suite-tags' }),
          formField('描述 description', descriptionArea, { id: 'suite-description' })
        ),
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
            el('div', { class: 'subform-title' }, '已选用例（按执行顺序）'),
            orderHost,
            el('div', { class: 'subform-title' }, '运行参数（套件级）'),
            suiteVarsHost,
            el(
              'details',
              { class: 'case-picker-details' },
              el('summary', {}, '选择用例（展开后勾选，共 ' + view.cases.length + ' 条候选）'),
              el(
                'div',
                { class: 'stack' },
                formField('搜索用例', searchInput, { id: 'suite-case-search' }),
                pickerBox
              )
            )
          )
        )
      )
    );

    renderPickerList();
    renderOrderList();
    renderSuiteVariables();
  }

  await loadEnvironments();
  await loadCases();
  await loadFolders();
  await loadSuites();
  renderEditor();
}
