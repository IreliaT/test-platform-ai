/**
 * 环境：GET/POST /api/v1/projects/{pid}/environments
 * CRUD + 变量键值编辑器 + 认证配置 + 设为默认（POST …/environments/{id}/default）。
 *
 * 掩码值 ****** 显示为「保持原值」，未修改时原样回传（HTTP_API.md §6）。
 */

import { Api } from '../api.js';
import {
  MASK_VALUE,
  authEditor,
  clear,
  confirmDialog,
  el,
  emptyBlock,
  errorBanner,
  formField,
  icon,
  itemsOf,
  loadingBlock,
  runAction,
  textControl,
  toast,
} from '../app.js';

/** 与 hapitest/redact.py 的敏感词保持一致，仅用于界面提示。 */
const SECRET_KEYWORDS = [
  'password', 'passwd', 'pwd', 'secret', 'token', 'access_key', 'accesskey', 'api_key', 'apikey',
  'app_secret', 'appsecret', 'private_key', 'privatekey', 'authorization', 'auth', 'cookie',
  'session', 'credential', 'signature', 'sign', 'salt',
];

function isSecretKey(name) {
  const lower = String(name || '').toLowerCase();
  return SECRET_KEYWORDS.some((keyword) => lower.indexOf(keyword) >= 0);
}

function emptyEnvironment() {
  return {
    id: null,
    name: '默认环境',
    base_url: '',
    variables: {},
    auth: { type: 'inherit', token: '', username: '', password: '', key: '', value: '', location: 'header', scheme: 'Bearer' },
    is_default: false,
  };
}

/** 环境变量编辑器：对象 <-> 行列表，掩码值显示“保持原值”。 */
function variablesEditor(variables) {
  const rows = Object.keys(variables || {}).map((key) => {
    const masked = isSecretKey(key) && variables[key] === MASK_VALUE;
    return {
      originalName: key,
      originalValue: variables[key],
      masked,
      touched: false,
      name: key,
      value: masked || variables[key] === null || variables[key] === undefined ? '' : String(variables[key]),
    };
  });
  const body = el('tbody');
  const table = el(
    'table',
    { class: 'kv-table' },
    el(
      'thead',
      {},
      el(
        'tr',
        {},
        el('th', { text: '变量名' }),
        el('th', { text: '值' }),
        el('th', { text: '敏感' }),
        el('th', { class: 'kv-actions', text: '操作' })
      )
    ),
    body
  );

  function renderRows(focusLast) {
    clear(body);
    let lastInput = null;
    rows.forEach((row, index) => {
      const badge = el('span', { class: 'secret-badge', hidden: !isSecretKey(row.name) }, icon('i-lock', 'icon-sm'), '敏感');
      const nameInput = textControl({
        value: row.name,
        placeholder: '变量名，如 base_url',
        ariaLabel: '变量名 ' + (index + 1),
        on: {
          input: (event) => {
            row.name = event.target.value;
            badge.hidden = !isSecretKey(row.name);
          },
        },
      });
      const valueInput = textControl({
        value: row.masked ? '' : row.value,
        placeholder: row.masked ? '保持原值' : '变量值',
        ariaLabel: '变量值 ' + (index + 1),
        on: {
          input: (event) => {
            row.value = event.target.value;
            row.touched = true;
          },
        },
      });
      lastInput = nameInput;
      body.appendChild(
        el(
          'tr',
          {},
          el('td', {}, nameInput),
          el(
            'td',
            {},
            valueInput,
            row.masked ? el('span', { class: 'keep-badge', text: '保持原值' }) : null
          ),
          el('td', { class: 'nowrap' }, badge),
          el(
            'td',
            { class: 'kv-actions' },
            el('button', {
              type: 'button',
              class: 'btn btn-ghost btn-sm btn-icon',
              'aria-label': '删除变量 ' + (row.name || index + 1),
              on: {
                click: () => {
                  rows.splice(index, 1);
                  renderRows();
                },
              },
            }, icon('i-trash', 'icon-sm'))
          )
        )
      );
    });
    if (!rows.length) {
      body.appendChild(
        el('tr', {}, el('td', { colspan: '4' }, el('span', { class: 'field-hint', text: '还没有变量，点击「新增变量」添加。' })))
      );
    }
    if (focusLast && lastInput) lastInput.focus();
  }

  renderRows();
  const node = el(
    'div',
    { class: 'stack' },
    table,
    el(
      'div',
      { class: 'row' },
      el('button', {
        type: 'button',
        class: 'btn btn-sm',
        on: {
          click: () => {
            rows.push({ originalName: '', originalValue: undefined, masked: false, name: '', value: '' });
            renderRows(true);
          },
        },
      }, icon('i-plus', 'icon-sm'), '新增变量'),
      el('span', {
        class: 'field-hint',
        text: '名称含 password / token / secret 等关键词的变量会被后端掩码，界面显示为 ******。',
      })
    )
  );

  return {
    node,
    getValue() {
      const result = {};
      rows.forEach((row) => {
        const name = String(row.name || '').trim();
        if (!name) return;
        if (row.masked && !row.touched) {
          // 未修改的敏感值：原样回传掩码，后端保持原值
          result[name] = MASK_VALUE;
          return;
        }
        if (!row.touched && row.originalName === name && row.originalValue !== undefined && row.originalValue !== MASK_VALUE) {
          // 未改动且名称未变：保留原始值与类型
          result[name] = row.originalValue;
          return;
        }
        result[name] = row.value;
      });
      return result;
    },
  };
}

export async function render(ctx) {
  const pid = ctx.pid;
  const view = { environments: [], current: null };

  const main = ctx.container;
  clear(main);
  main.appendChild(
    el(
      'div',
      { class: 'page-head' },
      el('h1', { text: '环境' }),
      el('button', {
        type: 'button',
        class: 'btn btn-primary',
        on: { click: () => selectEnvironment(emptyEnvironment(), true) },
      }, icon('i-plus'), '新建环境'),
      el('div', { class: 'page-sub', text: '环境提供 base_url、变量与认证；运行时可覆盖套件默认环境。' })
    )
  );

  const listHost = el('div', { class: 'picker' });
  const listCard = el(
    'section',
    { class: 'card' },
    el('div', { class: 'card-head' }, el('h2', { text: '环境列表' }), el('span', { class: 'pill-count', id: 'env-count' })),
    listHost
  );
  const editorHost = el('section', { class: 'card' });
  main.appendChild(el('div', { class: 'split' }, listCard, editorHost));

  function renderList() {
    clear(listHost);
    const countNode = document.getElementById('env-count');
    if (countNode) countNode.textContent = String(view.environments.length);
    if (!view.environments.length) {
      listHost.appendChild(emptyBlock('还没有环境', '点击「新建环境」配置 base_url 与变量。'));
      return;
    }
    const list = el('ul', { class: 'picker-list' });
    view.environments.forEach((env) => {
      const current = view.current && view.current.id === env.id;
      const item = el(
        'li',
        {},
        el(
          'button',
          {
            type: 'button',
            class: 'link-btn',
            style: { textAlign: 'left', flex: '1', minWidth: '0' },
            'aria-current': current ? 'true' : null,
            on: { click: () => selectEnvironment(env, false) },
          },
          el('span', { text: env.name || '(未命名环境)' }),
          el(
            'span',
            { class: 'row tight' },
            env.is_default ? el('span', { class: 'chip chip-passed', text: '默认' }) : null,
            el('span', { class: 'faint mono break-all', text: env.base_url || '（未设置 base_url）' })
          )
        ),
        env.is_default
          ? null
          : el('button', {
              type: 'button',
              class: 'btn btn-sm',
              text: '设为默认',
              on: { click: () => setDefault(env) },
            })
      );
      if (current) item.setAttribute('aria-selected', 'true');
      list.appendChild(item);
    });
    listHost.appendChild(list);
  }

  async function loadEnvironments() {
    clear(listHost);
    listHost.appendChild(loadingBlock('正在加载环境…'));
    try {
      const data = await Api.listEnvironments(pid, { signal: ctx.signal });
      view.environments = itemsOf(data);
    } catch (err) {
      clear(listHost);
      listHost.appendChild(errorBanner(err));
      return;
    }
    renderList();
  }

  function setHashSilently(hash) {
    try {
      window.history.replaceState(null, '', hash);
    } catch (err) {
      /* 忽略 */
    }
  }

  function selectEnvironment(env, isNew) {
    view.current = env;
    if (isNew) setHashSilently('#/p/' + encodeURIComponent(pid) + '/environments');
    else if (env.id) setHashSilently('#/p/' + encodeURIComponent(pid) + '/environments/' + encodeURIComponent(env.id));
    renderEditor();
    renderList();
  }

  async function setDefault(env) {
    const [ok] = await runAction(() => Api.setDefaultEnvironment(pid, env.id), '已将「' + env.name + '」设为默认环境');
    if (!ok) return;
    await loadEnvironments();
    if (view.current && view.current.id === env.id) {
      view.current.is_default = true;
      renderEditor();
    }
  }

  function renderEditor() {
    clear(editorHost);
    const env = view.current;
    if (!env) {
      editorHost.appendChild(emptyBlock('未选择环境', '从左侧选择环境，或点击「新建环境」。'));
      return;
    }
    const isNew = !env.id;

    const nameInput = textControl({ id: 'env-name', value: env.name || '' });
    const baseUrlInput = textControl({
      id: 'env-base-url',
      value: env.base_url || '',
      placeholder: 'http://127.0.0.1:8080',
    });
    const variables = variablesEditor(env.variables || {});
    const authBlock = authEditor(env.auth || {}, { legend: '认证（环境级，用例可继承）', masked: !isNew });

    function buildPayload() {
      const name = nameInput.value.trim();
      if (!name) throw new Error('环境名称不能为空');
      return {
        name,
        base_url: baseUrlInput.value.trim(),
        variables: variables.getValue(),
        auth: authBlock.getValue(),
        is_default: Boolean(env.is_default),
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
              () => (isNew ? Api.createEnvironment(pid, payload) : Api.updateEnvironment(pid, env.id, payload)),
              isNew ? '环境已创建' : '环境已保存'
            );
            if (!ok) return;
            const saved = (data && (data.environment || data.env)) || data;
            await loadEnvironments();
            if (saved && saved.id) selectEnvironment(saved, false);
            else if (isNew) selectEnvironment(emptyEnvironment(), true);
          },
        },
      },
      icon('i-check'),
      isNew ? '创建环境' : '保存'
    );

    editorHost.appendChild(
      el(
        'div',
        { class: 'card-head' },
        el('h2', { text: isNew ? '新建环境' : '编辑环境 ' + (env.id ? '#' + env.id : '') }),
        env.is_default ? el('span', { class: 'chip chip-passed', text: '默认环境' }) : null,
        el('div', { class: 'spacer' }),
        isNew || env.is_default
          ? null
          : el('button', {
              type: 'button',
              class: 'btn btn-sm',
              text: '设为默认',
              on: { click: () => setDefault(env) },
            }),
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
                      title: '删除环境',
                      message: '将删除环境「' + (env.name || env.id) + '」，引用它的套件需要重新指定环境。',
                      confirmLabel: '删除环境',
                    });
                    if (!confirmed) return;
                    const [ok] = await runAction(() => Api.deleteEnvironment(pid, env.id), '环境已删除');
                    if (ok) {
                      view.current = null;
                      setHashSilently('#/p/' + encodeURIComponent(pid) + '/environments');
                      await loadEnvironments();
                      renderEditor();
                    }
                  },
                },
              },
              icon('i-trash', 'icon-sm'),
              '删除'
            ),
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
          formField('环境名称 *', nameInput, { id: 'env-name' }),
          formField('base_url', baseUrlInput, { id: 'env-base-url', hint: '用例未覆盖 base_url 时使用' })
        ),
        el(
          'fieldset',
          { class: 'subform' },
          el('legend', { text: '环境变量 variables' }),
          variables.node
        ),
        authBlock.node,
        el('div', {
          class: 'field-hint',
          text: '保存时：值为 ****** 或留空的敏感变量表示“保持原值”，后端不会用掩码覆盖真实值。',
        })
      )
    );
  }

  await loadEnvironments();
  renderEditor();
}
