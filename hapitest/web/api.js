/**
 * HapiTest Web 控制台 —— HTTP 封装与轮询工具。
 *
 * 约定（见 docs/architecture/HTTP_API.md）：
 * - 所有接口以 /api/v1 为前缀，返回信封 { ok: true, data } 或 { ok: false, error }；
 * - 非 2xx 或 ok=false 时抛出 ApiError(message)，调用方只需展示 err.message；
 * - 列表响应形如 { items, total, limit, offset }；
 * - 运行详情以 800ms 间隔轮询，直到 status 不在 queued|running。
 */

export const API_PREFIX = '/api/v1';

/** 轮询间隔（毫秒），由 HTTP_API.md §10 固定为 800ms。 */
export const RUN_POLL_INTERVAL_MS = 800;

/** 非 queued/running 即视为终态。 */
export const TERMINAL_RUN_STATUSES = ['passed', 'failed', 'error', 'incomplete', 'canceled'];

export class ApiError extends Error {
  constructor(message, options = {}) {
    super(message);
    this.name = 'ApiError';
    this.code = options.code || 'internal_error';
    this.status = options.status || 0;
    this.details = options.details || [];
  }
}

function isAbsolutePath(path) {
  return typeof path === 'string' && path.indexOf('/api/') === 0;
}

/** 拼接 /api/v1 前缀与查询串；空值（'' / null / undefined）会被跳过。 */
export function apiUrl(path, query) {
  const base = isAbsolutePath(path) ? path : API_PREFIX + path;
  const search = new URLSearchParams();
  if (query && typeof query === 'object') {
    Object.keys(query).forEach((key) => {
      const value = query[key];
      if (value === undefined || value === null || value === '') return;
      if (Array.isArray(value)) {
        value.forEach((item) => search.append(key, String(item)));
      } else {
        search.append(key, String(value));
      }
    });
  }
  const text = search.toString();
  return text ? base + '?' + text : base;
}

/**
 * 统一请求封装。
 * @param {string} path 相对 /api/v1 的路径（也接受完整的 /api/... 路径）
 * @param {{method?:string, body?:any, query?:object, signal?:AbortSignal, headers?:object}} [options]
 * @returns {Promise<any>} 信封中的 data；无 data 时返回 null
 */
export async function api(path, options = {}) {
  const method = (options.method || 'GET').toUpperCase();
  const url = apiUrl(path, options.query);
  const init = {
    method,
    headers: Object.assign({ Accept: 'application/json' }, options.headers || {}),
    signal: options.signal,
    credentials: 'same-origin',
  };
  if (options.body !== undefined && options.body !== null && method !== 'GET' && method !== 'HEAD') {
    init.headers['Content-Type'] = 'application/json; charset=utf-8';
    init.body = typeof options.body === 'string' ? options.body : JSON.stringify(options.body);
  }

  let response;
  try {
    response = await fetch(url, init);
  } catch (err) {
    if (err && err.name === 'AbortError') throw err;
    throw new ApiError('无法连接 HapiTest 服务，请确认服务仍在运行。', { code: 'network_error' });
  }

  const text = await response.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch (err) {
      payload = null;
    }
  }

  if (!response.ok || (payload && payload.ok === false)) {
    const error = (payload && payload.error) || {};
    const message =
      error.message ||
      (response.status === 404
        ? '资源不存在（404）'
        : '请求失败（HTTP ' + response.status + '）');
    throw new ApiError(message, {
      code: error.code || 'http_' + response.status,
      status: response.status,
      details: error.details || [],
    });
  }

  if (payload && typeof payload === 'object' && 'data' in payload) return payload.data;
  return payload;
}

export const http = {
  get: (path, options) => api(path, Object.assign({}, options, { method: 'GET' })),
  post: (path, body, options) => api(path, Object.assign({}, options, { method: 'POST', body })),
  put: (path, body, options) => api(path, Object.assign({}, options, { method: 'PUT', body })),
  del: (path, options) => api(path, Object.assign({}, options, { method: 'DELETE' })),
};

/**
 * 轮询helper：立即执行一次，若未完成则每隔 intervalMs 再执行，直到 isDone 为真。
 * @param {() => Promise<any>} fn 取数函数
 * @param {{intervalMs?:number, isDone?:(data:any)=>boolean, onData?:(data:any)=>void,
 *          onError?:(err:any)=>void, signal?:AbortSignal}} [options]
 * @returns {() => void} 停止函数
 */
export function poll(fn, options = {}) {
  const intervalMs = options.intervalMs || RUN_POLL_INTERVAL_MS;
  const isDone = options.isDone || (() => true);
  const signal = options.signal;
  let stopped = false;
  let timer = null;

  function stop() {
    stopped = true;
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  }

  async function tick() {
    if (stopped || (signal && signal.aborted)) return;
    let data;
    try {
      data = await fn();
    } catch (err) {
      if (err && err.name === 'AbortError') return stop();
      if (options.onError) options.onError(err);
      return stop();
    }
    if (stopped || (signal && signal.aborted)) return;
    try {
      if (options.onData) options.onData(data);
    } catch (err) {
      return stop();
    }
    if (isDone(data)) return stop();
    timer = setTimeout(tick, intervalMs);
  }

  if (signal) {
    if (signal.aborted) return stop;
    signal.addEventListener('abort', stop, { once: true });
  }
  tick();
  return stop;
}

/** 运行是否处于非终态（需要继续轮询）。 */
export function isRunActive(run) {
  const status = run && run.status;
  return status === 'queued' || status === 'running';
}

/** 生成一次触发的幂等键。 */
export function newRequestId() {
  const random = Math.random().toString(16).slice(2, 10);
  return 'ui-' + Date.now().toString(36) + '-' + random;
}

function pidPath(pid) {
  return '/api/v1/projects/' + encodeURIComponent(pid);
}

/**
 * 单资源响应解包：后端部分接口返回 {case: {...}} / {run: {...}} / {response: {...}}，
 * 文档未统一规定，这里按“有包装就取包装，没有就用原值”处理，两种形状都能用。
 */
function pick(data, ...keys) {
  if (data && typeof data === 'object' && !Array.isArray(data)) {
    for (let i = 0; i < keys.length; i += 1) {
      const value = data[keys[i]];
      if (value !== undefined && value !== null && typeof value === 'object') return value;
    }
  }
  return data;
}

/* ------------------------------------------------------------------ 接口表 */

export const Api = {
  // 1. 平台级
  health: (options) => http.get('/api/v1/health', options),
  meta: (options) => http.get('/api/v1/meta', options),

  // 2. 项目
  listProjects: (options) => http.get('/api/v1/projects', options),
  createProject: (body) => http.post('/api/v1/projects', body),
  getProject: (pid) => http.get(pidPath(pid)),
  updateProject: (pid, body) => http.put(pidPath(pid), body).then((data) => pick(data, 'project')),
  deleteProject: (pid) => http.del(pidPath(pid), { query: { confirm: pid } }),
  projectStats: (pid, options) => http.get(pidPath(pid) + '/stats', options),

  // 3. 接口目录
  listEndpoints: (pid, query, options) => http.get(pidPath(pid) + '/endpoints', Object.assign({ query }, options)),
  createEndpoint: (pid, body) => http.post(pidPath(pid) + '/endpoints', body).then((data) => pick(data, 'endpoint')),
  getEndpoint: (pid, id, options) =>
    http.get(pidPath(pid) + '/endpoints/' + encodeURIComponent(id), options).then((data) => pick(data, 'endpoint')),
  updateEndpoint: (pid, id, body) =>
    http.put(pidPath(pid) + '/endpoints/' + encodeURIComponent(id), body).then((data) => pick(data, 'endpoint')),
  deleteEndpoint: (pid, id) => http.del(pidPath(pid) + '/endpoints/' + encodeURIComponent(id)),
  endpointToCase: (pid, id, body) =>
    http.post(pidPath(pid) + '/endpoints/' + encodeURIComponent(id) + '/to-case', body).then((data) => pick(data, 'case')),
  probeEndpoint: (pid, id, body) =>
    http.post(pidPath(pid) + '/endpoints/' + encodeURIComponent(id) + '/probe', body).then((data) => pick(data, 'response', 'result')),
  listFolders: (pid, options) => http.get(pidPath(pid) + '/folders', options),

  // 4. 用例
  listCases: (pid, query, options) => http.get(pidPath(pid) + '/cases', Object.assign({ query }, options)),
  createCase: (pid, body) => http.post(pidPath(pid) + '/cases', body).then((data) => pick(data, 'case')),
  getCase: (pid, id, options) =>
    http.get(pidPath(pid) + '/cases/' + encodeURIComponent(id), options).then((data) => pick(data, 'case')),
  updateCase: (pid, id, body) =>
    http.put(pidPath(pid) + '/cases/' + encodeURIComponent(id), body).then((data) => pick(data, 'case')),
  deleteCase: (pid, id) => http.del(pidPath(pid) + '/cases/' + encodeURIComponent(id)),
  duplicateCase: (pid, id, body) =>
    http.post(pidPath(pid) + '/cases/' + encodeURIComponent(id) + '/duplicate', body).then((data) => pick(data, 'case')),
  runCase: (pid, id, body) =>
    http.post(pidPath(pid) + '/cases/' + encodeURIComponent(id) + '/run', body).then((data) => pick(data, 'result', 'case_result')),

  // 5. 套件
  listSuites: (pid, query, options) => http.get(pidPath(pid) + '/suites', Object.assign({ query }, options)),
  createSuite: (pid, body) => http.post(pidPath(pid) + '/suites', body).then((data) => pick(data, 'suite')),
  getSuite: (pid, id, options) =>
    http.get(pidPath(pid) + '/suites/' + encodeURIComponent(id), options).then((data) => pick(data, 'suite')),
  updateSuite: (pid, id, body) =>
    http.put(pidPath(pid) + '/suites/' + encodeURIComponent(id), body).then((data) => pick(data, 'suite')),
  deleteSuite: (pid, id) => http.del(pidPath(pid) + '/suites/' + encodeURIComponent(id)),

  // 6. 环境
  listEnvironments: (pid, options) => http.get(pidPath(pid) + '/environments', options),
  createEnvironment: (pid, body) => http.post(pidPath(pid) + '/environments', body).then((data) => pick(data, 'environment')),
  getEnvironment: (pid, id, options) =>
    http.get(pidPath(pid) + '/environments/' + encodeURIComponent(id), options).then((data) => pick(data, 'environment')),
  updateEnvironment: (pid, id, body) =>
    http.put(pidPath(pid) + '/environments/' + encodeURIComponent(id), body).then((data) => pick(data, 'environment')),
  deleteEnvironment: (pid, id) => http.del(pidPath(pid) + '/environments/' + encodeURIComponent(id)),
  setDefaultEnvironment: (pid, id) =>
    http.post(pidPath(pid) + '/environments/' + encodeURIComponent(id) + '/default', {}).then((data) => pick(data, 'environment')),

  // 7. 运行
  createRun: (pid, body) => http.post(pidPath(pid) + '/runs', body).then((data) => pick(data, 'run', 'run_result')),
  listRuns: (pid, query, options) => http.get(pidPath(pid) + '/runs', Object.assign({ query }, options)),
  getRun: (pid, runId, options) =>
    http.get(pidPath(pid) + '/runs/' + encodeURIComponent(runId), options).then((data) => pick(data, 'run', 'run_result')),
  cancelRun: (pid, runId) => http.post(pidPath(pid) + '/runs/' + encodeURIComponent(runId) + '/cancel', {}),
  diffRun: (pid, runId, base, options) =>
    http.get(pidPath(pid) + '/runs/' + encodeURIComponent(runId) + '/diff', Object.assign({ query: { base } }, options)),
  reportUrl: (pid, runId, format) =>
    apiUrl(pidPath(pid) + '/runs/' + encodeURIComponent(runId) + '/report', { format }),

  // 8. 导入
  listImports: (pid, query, options) => http.get(pidPath(pid) + '/imports', Object.assign({ query }, options)),
  previewImport: (pid, body) => http.post(pidPath(pid) + '/imports/preview', body),
  createImport: (pid, body) => http.post(pidPath(pid) + '/imports', body),
  fetchImport: (pid, body) => http.post(pidPath(pid) + '/imports/fetch', body),
  // 通过 MCP 从 Apifox 同步（访问令牌只从服务端环境/.env.local 读取，前端不传令牌）
  mcpImport: (pid, body) => http.post(pidPath(pid) + '/imports/mcp', body),

  // 9. 导出（下载链接）
  exportUrl: (pid, reveal) => apiUrl(pidPath(pid) + '/export', reveal ? { reveal: 1 } : {}),
};
