"""导入落库与外部规格抓取。

- :func:`apply_import` 把导入器产出的接口/用例写进项目库，支持 ``merge`` 与 ``replace``；
- :func:`fetch_spec` 从运行中的服务抓取 OpenAPI/Swagger 文档（如 ``/v2/api-docs?group=api-3``）。

导入是幂等的：``merge`` 模式下按 ``method + path`` 更新接口，已存在的用例（按 ``source_ref``）
不会被重复创建，因此可以反复同步同一份契约。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import PlatformConfig
from .errors import ValidationError
from .importers import ImportOptions, parse_file, parse_text
from .models import CaseSpec, EndpointSpec, ImportResult, ImportStats, RequestSpec
from .repository import Repository

DETECTED_PREFIXES = ("detected_server_url:", "detected_application_name:", "detected_server_port:", "detected_context_path:")


def apply_import(
    repository: Repository,
    result: ImportResult,
    mode: str = "merge",
    refresh_cases: bool = False,
) -> ImportStats:
    """把导入结果写入项目库。

    ``refresh_cases=True`` 时，已存在的生成用例会**用契约里的请求模板与断言覆盖**，
    同时保留人工维护的字段（名称、目录、优先级、启停、标签、用例变量）。
    默认关闭：绝不悄悄改掉别人调过的用例。
    """
    if mode not in ("merge", "replace"):
        raise ValidationError("mode 只能是 merge 或 replace")
    stats = ImportStats(source=result.stats.source, source_ref=result.stats.source_ref)
    stats.warnings = list(result.stats.warnings) + quality_warnings(result)

    if mode == "replace":
        removed = repository.clear_endpoints()
        stats.warnings.append("replace 模式：已清空 %d 个既有接口" % removed)

    endpoint_id_by_signature: Dict[str, Optional[int]] = {}
    for endpoint in result.endpoints:
        saved, action = repository.upsert_endpoint(endpoint)
        endpoint_id_by_signature[saved.signature()] = saved.id
        if action == "created":
            stats.created += 1
        elif action == "updated":
            stats.updated += 1
        else:
            stats.unchanged += 1

    for case in result.cases:
        # 先确定用例属于哪个接口，再据此判重：
        # 契约可能被反复导出成不同文件名，"文档路径"不能作为幂等键。
        _bind_endpoint(repository, case, endpoint_id_by_signature)
        existing = _find_existing_case(repository, case)
        if existing is not None:
            if refresh_cases:
                _refresh_generated_parts(existing, case)
                repository.update_case(existing.id, existing)
                stats.cases_updated += 1
            else:
                stats.unchanged += 1
            continue
        repository.create_case(case)
        stats.cases_created += 1

    return stats


def _find_existing_case(repository: Repository, case: CaseSpec) -> Optional[CaseSpec]:
    """按 ``source + source_ref + name`` 找到已存在的用例。

    判重键里的 ``source_ref`` 在绑定阶段被改写为接口签名（``GET /api/users/{id}``）——
    这样即使用例来自不同文件名的同一份契约，重复同步也不会产生重复用例。
    """
    if not case.source_ref:
        return None
    cases, _ = repository.list_cases(limit=100000)
    for existing in cases:
        if existing.source == case.source and existing.source_ref == case.source_ref and existing.name == case.name:
            return existing
    return None


def _refresh_generated_parts(existing: CaseSpec, fresh: CaseSpec) -> None:
    """用新契约覆盖"生成的部分"，保留人工维护的字段。"""
    existing.request = fresh.request
    existing.assertions = list(fresh.assertions)
    existing.extractors = list(fresh.extractors)
    existing.endpoint_id = fresh.endpoint_id or existing.endpoint_id
    existing.description = fresh.description or existing.description


def _bind_endpoint(repository: Repository, case: CaseSpec, mapping: Dict[str, Optional[int]]) -> None:
    """把生成的用例关联到接口，并把 ``source_ref`` 规范成接口签名。

    生成用例时路径参数已被示例值替换（``/api/users/{id}`` → ``/api/users/1``），
    所以先精确匹配，再按路径模板回退。
    """
    if case.endpoint_id is not None:
        return
    endpoint = repository.find_endpoint_for_path(case.request.method, case.request.path)
    if endpoint is None:
        signature = "%s %s" % (case.request.method.upper(), case.request.path)
        if signature in mapping:
            case.endpoint_id = mapping[signature]
        return
    case.endpoint_id = endpoint.id
    case.source_ref = endpoint.signature()


def load_import(
    config: PlatformConfig,
    source: str = "auto",
    text: Optional[str] = None,
    path: Optional[str] = None,
    options: Optional[Dict[str, Any]] = None,
) -> ImportResult:
    """解析导入内容（不写库）。

    解析失败一律转成 :class:`ValidationError`，让 CLI 与 HTTP API 输出可读的错误，
    而不是把解析器内部的 ValueError 直接抛给调用方。
    """
    opts = build_options(options)
    try:
        if text and text.strip():
            result = parse_text(text, source_ref=(path or "<text>"), options=opts)
        elif path:
            candidate = Path(path).expanduser()
            if not candidate.is_absolute():
                candidate = (config.root_dir / candidate).resolve()
            if not candidate.exists():
                raise ValidationError("文件不存在: %s" % candidate)
            if candidate.is_dir():
                raise ValidationError("这是一个目录：源码扫描请使用 --scan，或指定具体的契约文件")
            result = parse_file(candidate, options=opts)
        else:
            raise ValidationError("必须提供 text 或 path")
    except ValidationError:
        raise
    except ValueError as exc:
        raise ValidationError("解析失败：%s" % exc)
    except OSError as exc:
        raise ValidationError("读取失败：%s" % exc)
    if source and source != "auto":
        _force_source(result, source)
    return result


def _is_placeholder_example(param) -> bool:
    """判断路径参数的示例值是不是"类型默认占位"，而不是真实业务数据。

    启发式：缺失/空串，或整数 0、浮点 0.0 —— 导入器在契约没有 ``example``/``default``/``enum`` 时
    会按类型生成这些值。它们几乎不可能对应真实业务主键，因此必须提示用户替换。
    明确写了 ``example: 0`` 的接口会被误报一次，代价可接受（提示不影响执行）。
    """
    if param.example is None or param.example == "":
        return True
    if param.type in ("integer",) and param.example == 0:
        return True
    if param.type in ("number",) and param.example in (0, 0.0):
        return True
    return False


def quality_warnings(result: ImportResult, limit: int = 20) -> List[str]:
    """对导入结果做质量提示：这些接口的冒烟用例需要人工补数据，否则必然失败。

    平台不能凭空知道业务数据（例如"某个真实存在的用户 ID"），但必须把这件事讲清楚，
    而不是让用户面对一堆莫名其妙的 404。
    """
    warnings: List[str] = []
    for endpoint in result.endpoints:
        missing = [
            param.name
            for param in endpoint.params
            if param.location == "path" and param.required and _is_placeholder_example(param)
        ]
        if missing:
            warnings.append(
                "接口 %s 的路径参数 %s 缺少示例值，生成的冒烟用例使用占位值，请按业务数据填写"
                % (endpoint.signature(), "、".join(missing))
            )
        if len(warnings) >= limit:
            warnings.append("……更多路径参数提示已省略")
            break
    for case in result.cases:
        if "{" in case.request.path:
            warnings.append("用例「%s」仍有未解析的路径参数：%s" % (case.name, case.request.path))
            if len(warnings) >= limit:
                break
    return warnings


def build_options(options: Optional[Dict[str, Any]]) -> ImportOptions:
    data = dict(options or {})
    return ImportOptions(
        base_url=str(data.get("base_url", "") or ""),
        folder_prefix=str(data.get("folder_prefix", "") or ""),
        generate_cases=bool(data.get("generate_cases", True)),
        case_suffix=str(data.get("case_suffix", " - 冒烟") or ""),
        include_deprecated=bool(data.get("include_deprecated", False)),
        default_headers=list(data.get("default_headers") or []),
        tags=list(data.get("tags") or []),
        server_url=str(data.get("server_url", "") or ""),
    )


def _force_source(result: ImportResult, source: str) -> None:
    result.source = source
    result.stats.source = source
    for endpoint in result.endpoints:
        endpoint.source = source
    for case in result.cases:
        case.source = source


def detected_hints(result: ImportResult) -> Dict[str, str]:
    """从 importers 的 warnings 中提取探测到的服务信息。"""
    hints: Dict[str, str] = {}
    for warning in result.stats.warnings:
        for prefix in DETECTED_PREFIXES:
            if warning.startswith(prefix):
                hints[prefix.rstrip(":")] = warning.split(":", 1)[1].strip()
    return hints


def fetch_spec(
    config: PlatformConfig,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout_ms: Optional[int] = None,
) -> Tuple[str, str, int]:
    """抓取远端规格文档，返回 (文本, 最终 URL, 字节数)。"""
    from .http_client import request as http_request
    from .models import KeyValue

    if not url or "://" not in url:
        raise ValidationError("url 必须是完整的 http(s) 地址")
    request_spec = RequestSpec(
        method="GET",
        path=url,
        headers=[KeyValue(name=str(k), value=str(v)) for k, v in (headers or {}).items()],
        timeout_ms=timeout_ms or max(5000, config.default_timeout_ms),
    )
    response = http_request(request_spec, config)
    if not response.ok:
        raise ValidationError("抓取失败: %s" % (response.error or "未知错误"))
    if response.status and response.status >= 400:
        raise ValidationError("抓取失败: HTTP %s（请确认地址与鉴权，例如 /v2/api-docs?group=xxx）" % response.status)
    text = response.body_text or ""
    if text.lstrip().startswith("<"):
        raise ValidationError("返回内容看起来是 HTML 页面而不是接口文档，请检查地址或鉴权（Spring Security 可能返回登录页）")
    return text, response.url or url, len(text.encode("utf-8"))


def apifox_token(project_root: Path, token_env: str = "APIFOX_ACCESS_TOKEN") -> Optional[str]:
    """读取 Apifox 访问令牌：进程环境优先，其次项目根目录的 .env.local / .env。

    只读取、不写入；`.env.local` 已在 .gitignore 中排除。
    """
    import os

    from .config import load_env_file

    value = os.environ.get(token_env)
    if value:
        return value.strip()
    for name in (".env.local", ".env"):
        values = load_env_file(Path(project_root) / name)
        if values.get(token_env):
            return str(values[token_env]).strip()
    return None


def load_import_from_mcp(
    config: PlatformConfig,
    project_id: Optional[str] = None,
    doc_id: Optional[str] = None,
    spec_file: Optional[str] = None,
    command: Optional[Sequence[str]] = None,
    token: Optional[str] = None,
    token_env: str = "APIFOX_ACCESS_TOKEN",
    refresh: bool = True,
    timeout_s: float = 180.0,
    api_base_url: Optional[str] = None,
    branch_id: Optional[str] = None,
    options: Optional[Dict[str, Any]] = None,
) -> Tuple[ImportResult, Dict[str, Any]]:
    """通过 MCP 从 Apifox 取回接口并解析（不写库）。

    返回 ``(ImportResult, meta)``；meta 里带服务端版本、使用的工具名、是否刷新过，
    便于回答"这次同步拿到的是不是最新数据"。
    """
    from .mcp_client import fetch_apifox_openapi

    opts = build_options(options)
    warnings: List[str] = []
    resolved_token = token or apifox_token(config.root_dir, token_env)
    text, meta = fetch_apifox_openapi(
        project_id=project_id,
        doc_id=doc_id,
        spec_file=spec_file,
        command=command,
        token=resolved_token,
        token_env=token_env,
        refresh=refresh,
        timeout_s=timeout_s,
        api_base_url=api_base_url,
        branch_id=branch_id,
        warnings=warnings,
    )
    source_ref = "mcp://apifox/%s" % (project_id or doc_id or spec_file or "unknown")
    try:
        result = parse_text(text, source_ref=source_ref, options=opts)
    except ValueError as exc:
        raise ValidationError("MCP 返回的内容无法解析为接口文档：%s" % exc)
    _force_source(result, "apifox")
    result.stats.warnings = list(result.stats.warnings) + warnings
    meta["bytes"] = len(text.encode("utf-8"))
    meta["parsed_endpoints"] = len(result.endpoints)
    return result, meta


def seed_environment_base_url(repository: Repository, result: ImportResult, base_url: str) -> None:
    """把导入时探测到的 base_url 写入默认环境（仅当环境为空时）。"""
    if not base_url:
        return
    environment = repository.default_environment()
    if environment is None:
        return
    if environment.base_url:
        return
    environment.base_url = base_url
    repository.update_environment(environment.id, environment)
