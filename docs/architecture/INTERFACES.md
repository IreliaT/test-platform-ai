# HapiTest 内部接口契约（冻结草案 v1）

> 本文件是并行开发的唯一接口事实源。所有模块只能使用 `hapitest/models.py` 中定义的字段名，
> 不得自行新增别名字段。若确需扩展，先改本文件与 `models.py`，再改实现。

## 0. 工程约束

- Python 3.9+，**只允许标准库**（`sqlite3`、`http.server`、`urllib`、`json`、`re`、`html`、`unittest`…）。
  仅在「已安装则使用、未安装则降级」的场景允许 `import yaml` 这类可选导入。
- 每个模块 `from __future__ import annotations`，类型注解用 `typing`。
- 注释与用户可见文案使用中文；标识符使用英文 snake_case。
- 时间统一用 `hapitest.models.now_iso()`（UTC，`Z` 结尾）。
- 脱敏统一走 `hapitest/redact.py`：任何写库、返回给界面、写入日志的请求内容必须先脱敏。

## 1. 模块清单与公开接口

### 1.1 `hapitest/jsonpath.py`

```python
class JsonPathError(ValueError): ...

def query(data: Any, expression: str) -> List[Any]      # 无匹配返回 []，语法错误抛 JsonPathError
def first(data: Any, expression: str, default: Any = None) -> Any
def exists(data: Any, expression: str) -> bool
```

支持的语法子集：

| 语法 | 说明 |
|---|---|
| `$` | 根 |
| `.name` / `['name']` / `["name"]` | 子字段 |
| `[0]` / `[-1]` | 数组下标（负数为倒数） |
| `[*]` / `.*` | 数组/对象全展开 |
| `..name` | 递归下降查找字段 |
| `[0:2]` / `[1:]` / `[:2]` | 数组切片 |
| `[?(@.status == 1)]` | 过滤：`== != > >= < <= =~`，支持 `&&` `||`，右侧可为数字、单双引号字符串、true/false/null |
| 省略 `$` | 自动补 `$.` |

### 1.2 `hapitest/assertions.py`

```python
@dataclass
class AssertionContext:
    status: Optional[int] = None
    headers: Dict[str, str] = field(default_factory=dict)   # 取值时不区分大小写
    cookies: Dict[str, str] = field(default_factory=dict)
    body_text: str = ""
    body_json: Any = None
    duration_ms: int = 0
    url: str = ""
    variables: Dict[str, Any] = field(default_factory=dict)

def build_context(response: "HttpResponse", variables: Optional[Dict[str, Any]] = None) -> AssertionContext
def evaluate(assertion: "AssertionSpec", context: AssertionContext, index: int = 0) -> "AssertionResult"
def evaluate_all(assertions: Sequence["AssertionSpec"], context: AssertionContext) -> List["AssertionResult"]
def compare(actual: Any, operator: str, expected: Any, tolerance: Optional[float] = None) -> Tuple[bool, str]
def default_assertions_for(method: str, response_schema: Any = None) -> List["AssertionSpec"]  # 供导入器生成
```

要求：

- `evaluate` 永不抛异常；无法取值时产出 `passed=False` 且 `message` 说明原因（例如「路径无匹配」）。
- `response_time` 断言取 `context.duration_ms`；`variable` 断言取 `context.variables[name]`。
- `compare` 的数值比较在两侧都能转 float 时按数值比较，否则按字符串比较；`approx` 使用 `tolerance`（默认 1e-6 相对误差）。
- `json_contains` 表示「期望是实际值的子集」；`json_equals` 表示结构相等（忽略字典键顺序）。
- `type_is` 的 expected 取 `string|number|integer|boolean|object|array|null`。

### 1.3 `hapitest/jsonschema_lite.py`

```python
def validate(instance: Any, schema: Any, path: str = "$") -> List[str]   # 返回错误列表，空即通过
```

支持 `type`、`required`、`properties`、`items`、`enum`、`nullable`、`oneOf`/`anyOf`/`allOf`（尽力而为）。

### 1.4 `hapitest/variables.py`

```python
VAR_PATTERN: "re.Pattern"     # 匹配 ${name}、{{name}}、${name:-default}

class VariableResolver:
    def __init__(self, layers: Optional[Sequence[Dict[str, Any]]] = None) -> None
    def add_layer(self, values: Mapping[str, Any], name: str = "") -> None   # 先加入的层优先级更高
    def set(self, key: str, value: Any) -> None
    def get(self, key: str, default: Any = None) -> Any
    def resolve_text(self, text: str) -> str
    def resolve_value(self, value: Any) -> Any            # 递归解析 str/list/dict
    def resolve_mapping(self, values: Mapping[str, Any]) -> Dict[str, Any]
    def unresolved_names(self) -> List[str]
    def missing_names(self) -> List[str]                  # 解析时未命中的变量名
    def as_dict(self) -> Dict[str, Any]

def build_resolver(*layers: Mapping[str, Any]) -> VariableResolver
def extract_value(response: "HttpResponse", extractor: "ExtractorSpec") -> Tuple[bool, Any, str]
def apply_extractors(response: "HttpResponse", extractors: Sequence["ExtractorSpec"],
                     resolver: VariableResolver) -> Tuple[Dict[str, Any], List[str]]
```

未命中变量的处理：保留原文并记录到 `missing_names()`，由执行引擎决定是否记 warning。

### 1.5 `hapitest/http_client.py`

```python
class RequestBlocked(Exception): ...

@dataclass
class PreparedRequest:
    method: str
    url: str
    headers: Dict[str, str]
    body: Optional[bytes]
    timeout_ms: int
    follow_redirects: bool
    verify_tls: bool
    preview: Dict[str, Any]          # 已脱敏，可直接落库

def prepare(request: "RequestSpec", base_url: str = "", resolver: Optional[VariableResolver] = None) -> PreparedRequest
def send(prepared: PreparedRequest, config: PlatformConfig, cookie_jar: Optional[Dict[str, str]] = None) -> "HttpResponse"
def request(request: "RequestSpec", config: PlatformConfig, base_url: str = "",
            resolver: Optional[VariableResolver] = None,
            cookie_jar: Optional[Dict[str, str]] = None) -> "HttpResponse"
def check_url_allowed(url: str, config: PlatformConfig) -> Optional[str]   # 返回拦截原因，None 表示放行
```

要求：

- `prepare` 完成变量替换、默认头（`User-Agent: HapiTest/<version>`、`Accept: */*`）、认证注入、
  查询参数拼接（`enabled=True` 才带上，值为空也保留 `?k=`）、路径参数替换。
- 请求失败不抛异常给调用方：返回 `HttpResponse(ok=False, error="...")`，`status=None`。
- 响应体按 `config.capture_body_bytes` 截断并置 `truncated=True`；`size_bytes` 为**实际读取到的字节数**，
  被截断时它是下界（真实长度可能更大）。
- 支持 `json`/`form`/`multipart`/`raw`/`none` 五种 body；`Content-Type` 未显式给出时按 body_type 补默认值。
- 自动解压 gzip/deflate；处理 `Set-Cookie` 写入 `cookie_jar`。
- 重定向：`follow_redirects=False` 时不跟随；最多跟随 5 次。
- `verify_tls=False` 时使用未校验的 SSL context（`ssl._create_unverified_context()`）。
- 只使用 `http.client`/`urllib`；实现需兼容 `config.connect_timeout_ms`（连接超时）。
- `preview` 字段：`{"method","url","headers","body","body_type"}`，其中敏感头/参数已脱敏。

### 1.5a 执行器异步流程扩展（向后兼容）

`StepSpec` 在既有 setup JSON 中增加字段，无需 SQLite 迁移：

- `poll_timeout_ms=0`：只请求一次；大于 0 时等待步骤断言及必填提取成功，上限 300000 ms。
- `poll_interval_ms=1000`、`poll_read_only=false`：GET/HEAD 可轮询，POST 查询须显式声明只读。写操作不应配置轮询。
- `required_variables=[]`：发送该步骤之前检查条件，缺少值返回 incomplete。

轮询的失败尝试不提交提取变量；必填提取失败会阻断后续步骤。HTTP 错误、请求构建/网络错误立即结束；业务结果尚未就绪可继续轮询。轮询取消检查间隔不超过 100 ms（正在发送的 HTTP 受请求超时约束）。步骤记录增加 `attempts`。

执行器对提取表达式、断言 path/expected 解析变量。`json_path_all` 返回 JSONPath 全部匹配组成的数组（无匹配为空数组），用于唯一性断言。内置 `_case_started_epoch` 为开始前 5 秒，`_now_epoch` 每次请求更新到当前秒 + 1（查询上限不包含边界）。

`AuthSpec.type=aliyun_sls` 使用 username/password/token 表示 AccessKey ID/Secret/可选 STS Token；在 URL 和 body 编译完成后按 SLS 0.6.0 签名。SLS 请求不跟随重定向。认证响应 JSON 文本、提取值及敏感字段断言均在记录前脱敏。

未显式指定运行 env_id 时，使用套件绑定环境，再回退项目默认环境；显式环境优先。failure_strategy=stop 对 failed/error/incomplete/canceled 均停止。

运行参数优先级为本次 `RunRequest.variables` → 用例变量 → 环境变量。真实运行参数只留在内存中供执行使用，运行完成后清理；磁盘参数快照按敏感字段规则脱敏，幂等参数签名以 SHA-256 摘要保存，并兼容读取既有原始签名。重启后的快照不能恢复临时密钥，需要重新提供。

业务包 `hapitest.packs.airwallex_pk.install(repository)` 幂等同步测试资产，不执行业务 HTTP。完整说明见 [AIRWALLEX_PK.md](../testing/AIRWALLEX_PK.md)。

### 1.6 `hapitest/report.py`

```python
def to_junit_xml(run: "RunResult") -> str
def to_json_report(run: "RunResult") -> str
def to_html_report(run: "RunResult", project_name: str = "", title: str = "") -> str
def write_reports(run: "RunResult", directory: Path, project_name: str = "") -> Dict[str, str]   # {"xml": path, ...}
def summarize_text(run: "RunResult") -> str
```

- JUnit XML 必须可被 Jenkins/GitLab 解析：`<testsuites><testsuite name tests failures errors skipped time timestamp>`，
  每条用例 `<testcase classname="<folder>" name time>`，失败用 `<failure message type>`、异常用 `<error>`、跳过用 `<skipped/>`。
- HTML 报告自包含（内联 CSS、不引用外部资源、无 CDN），必须 `html.escape` 所有动态内容。

### 1.7 `hapitest/importers/`

```python
@dataclass
class ImportOptions:
    base_url: str = ""
    folder_prefix: str = ""
    generate_cases: bool = True
    case_suffix: str = " - 冒烟"
    include_deprecated: bool = False
    default_headers: List[str] = field(default_factory=list)   # "Name: value"
    tags: List[str] = field(default_factory=list)
    server_url: str = ""       # OpenAPI servers[0].url，用于推导 base_url

def parse_openapi(document: Any, source_ref: str = "", options: Optional[ImportOptions] = None) -> "ImportResult"
def parse_postman(document: Any, source_ref: str = "", options: Optional[ImportOptions] = None) -> "ImportResult"
def parse_apifox(document: Any, source_ref: str = "", options: Optional[ImportOptions] = None) -> "ImportResult"
def parse_curl(command: str, options: Optional[ImportOptions] = None) -> "ImportResult"
def scan_spring_project(root: Path, options: Optional[ImportOptions] = None) -> "ImportResult"

def parse_text(text: str, source_ref: str = "", options: Optional[ImportOptions] = None) -> "ImportResult"   # 自动识别格式
def parse_file(path: Path, options: Optional[ImportOptions] = None) -> "ImportResult"
def detect_format(text: str) -> str    # openapi | swagger2 | postman | apifox | curl | spring | unknown
```

`ImportResult.endpoints` 必须填好：`method`、`path`、`name`、`folder`（取 tag 或目录路径）、
`params`（含 location/required/type/example）、`request`（含示例 body、默认 header）、`source`、`source_ref`、
`operation_id`、`summary`、`deprecated`。

`generate_cases=True` 时为每个接口生成一条冒烟用例，`request` 复制接口模板，`assertions` 用
`assertions.default_assertions_for(method, response_schema)`，`name = "<接口名><case_suffix>"`，
`source` 与接口一致，`source_ref` 填 `method + path`。

生成示例 body 的规则（OpenAPI）：优先 `example` → `default` → `enum[0]` → 按 `type` 生成
（string: `""` 或按 `format` 给 date/date-time/uuid 示例；integer/number: 0；boolean: false；
array: 单元素；object: 递归），`required` 字段全部生成，非必填字段默认也生成但值为空/占位，循环 `$ref` 需断链。

### 1.8 `hapitest/web/`（前端）

纯静态：`index.html`、`styles.css`、`app.js`（可用 ES module 拆多个文件），
无构建步骤、无 CDN、离线可用。所有接口调用见 `docs/architecture/HTTP_API.md`。

## 2. 依赖方向

```
models.py  ← 所有模块
redact.py  ← importers / http_client / report / repository / server
jsonpath.py ← assertions.py, variables.py
http_client.py ← runner.py
variables.py + assertions.py + http_client.py ← runner.py
report.py ← runner.py / server
importers/* ← server / cli / repository
db.py + models.py ← repository.py ← runner.py / server / cli
```

禁止反向依赖：`models.py` 不得 import 其它业务模块；`importers/*` 不得 import `repository`。

## 3. 测试约定

- 测试放 `tests/`，用 `unittest`，命名 `test_<module>.py`，`python3 -m unittest discover -s tests -t .` 可跑。
- 不依赖网络：需要 HTTP 时使用 `http.server` 起本地临时服务（见 `tests/support.py`）。

## 4. 实现与本文档的已确认差异（实现为准）

各模块实现过程中确认的偏差，均已写入对应模块 docstring 并有测试覆盖：

| 位置 | 差异 | 原因 |
|---|---|---|
| `jsonpath` | 不提供 `set_value` | 递归下降/过滤/切片下的写语义不确定，模块只读 |
| `http_client.preview` | 仅在存在未解析路径参数时多一个 `unresolved_path_params` 键 | 便于排查，不影响固定 5 键消费方 |
| `http_client` | 每次重定向都重新执行 `check_url_allowed`；303 与 POST 的 301/302 降级为 GET | 防止 SSRF 绕过与错误的方法语义 |
| `http_client` | multipart 只支持文本字段 | 文件上传不在本期范围 |
| `redact.redact_url` | 掩码会被 URL 编码（`%2A%2A...`） | `urlencode` 的必然结果，断言时先 `unquote` |
| `assertions` | `json_schema` 断言忽略 `operator`，用 `expected` 作为 schema 校验 `path`；`enabled=false` 的断言返回 passed 并附说明 | 语义更直观，且避免"禁用即失败" |
| `assertions` | header/cookie 断言在 `name` 为空时回退用 `path` 作为头名 | 兼容只填一个字段的界面操作 |
| `jsonschema_lite` | 未知 `type` 名称静默忽略，错误最多 50 条 | OpenAPI 存在 `file` 等非 JSON Schema 类型 |
| `variables.apply_extractors` | 敏感名变量在执行器内保存明文，返回给历史的字典被掩码 | 执行需要真值，落库必须脱敏 |
| `runner` | 记录到 `CaseResult.request` 的字典额外带 `folder` | 报告需要按分组生成 JUnit `classname` |
| `runner` | 每次运行共享一个 Cookie Jar；并发套件为每条用例单独创建执行器 | 支持登录串联，同时避免并发下的会话串扰 |
| `runner` | `CaseResult.logs` 逐条带 ISO 时间戳，记录用例开始、环境与 base_url、请求行/请求头/请求体、响应状态与响应体、逐条断言、提取变量、重试与终态；运行级 `RunResult.logs` 追加每条用例的结果概览 | 让"跑了什么、回了什么、为什么这么判定"在界面上自解释，不依赖外部日志 |
