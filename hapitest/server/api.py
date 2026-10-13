"""HTTP API 的具体实现（见 docs/architecture/HTTP_API.md）。"""

from __future__ import annotations

import shlex
import time
from typing import Any, Dict, List, Optional

from .. import PLATFORM_NAME, PROTOCOL_VERSION, __version__
from ..errors import ConflictError, NotFoundError, ValidationError
from ..history import diff_runs
from ..models import (
    ASSERTION_TYPES,
    AUTH_TYPES,
    BODY_TYPES,
    CASE_SOURCES,
    CASE_STATUSES,
    EXTRACTOR_SCOPES,
    EXTRACTOR_SOURCES,
    HTTP_METHODS,
    OPERATORS,
    PRIORITIES,
    RUN_STATUSES,
    RUN_TRIGGERS,
    CaseSpec,
    EndpointSpec,
    EnvironmentSpec,
    RequestSpec,
    SuiteSpec,
    now_iso,
    validate_case,
)
from ..sync import (
    apply_import,
    detected_hints,
    fetch_spec,
    load_import,
    quality_warnings,
    seed_environment_base_url,
)
from .app import App, Request, Response, Router

PID = r"(?P<pid>[a-z0-9][a-z0-9_-]{0,62})"
RID = r"(?P<rid>[A-Za-z0-9_-]{1,64})"


# --------------------------------------------------------------------- 工具
def _project(app: App, pid: str):
    record = app.registry.get(pid)
    return record, app.registry.repository(pid)


def _spec(payload: Dict[str, Any]) -> Dict[str, Any]:
    inner = payload.get("spec")
    return inner if isinstance(inner, dict) else payload


def _paginate(request: Request) -> Dict[str, int]:
    return {
        "limit": request.int_param("limit", 50, minimum=1, maximum=500),
        "offset": request.int_param("offset", 0, minimum=0),
    }


def _list_payload(items: List[Any], total: int, limit: int, offset: int) -> Dict[str, Any]:
    return {"items": items, "total": total, "limit": limit, "offset": offset}


# --------------------------------------------------------------------- 平台
def health(app: App, request: Request) -> Dict[str, Any]:
    return {
        "name": PLATFORM_NAME,
        "version": __version__,
        "protocol": PROTOCOL_VERSION,
        "time": now_iso(),
        "uptime_s": int(time.time() - app.started_at),
        "data_dir": str(app.config.data_dir),
        "projects": len(app.registry.list_projects()),
    }


def meta(app: App, request: Request) -> Dict[str, Any]:
    from ..importers import SOURCE_LABELS

    return {
        "name": PLATFORM_NAME,
        "version": __version__,
        "protocol": PROTOCOL_VERSION,
        "http_methods": list(HTTP_METHODS),
        "body_types": list(BODY_TYPES),
        "auth_types": list(AUTH_TYPES),
        "assertion_types": list(ASSERTION_TYPES),
        "operators": list(OPERATORS),
        "priorities": list(PRIORITIES),
        "case_statuses": list(CASE_STATUSES),
        "run_statuses": list(RUN_STATUSES),
        "run_triggers": list(RUN_TRIGGERS),
        "case_sources": list(CASE_SOURCES),
        "extractor_sources": list(EXTRACTOR_SOURCES),
        "extractor_scopes": list(EXTRACTOR_SCOPES),
        "import_sources": ["auto"] + sorted(SOURCE_LABELS.keys()),
        "import_source_labels": SOURCE_LABELS,
        "limits": {"default_timeout_ms": app.config.default_timeout_ms, "max_workers": app.config.max_workers},
    }


# --------------------------------------------------------------------- 项目
def list_projects(app: App, request: Request) -> Dict[str, Any]:
    items = []
    for record in app.registry.list_projects():
        payload = record.to_dict()
        try:
            payload["counts"] = app.registry.repository(record.id).counts()
        except Exception:
            payload["counts"] = {}
        items.append(payload)
    return {"items": items, "total": len(items)}


def create_project(app: App, request: Request) -> Dict[str, Any]:
    payload = request.json()
    record = app.registry.create(
        name=str(payload.get("name", "") or ""),
        project_id=(str(payload["id"]) if payload.get("id") else None),
        description=str(payload.get("description", "") or ""),
        base_url=str(payload.get("base_url", "") or ""),
    )
    return {"project": record.to_dict(), "counts": app.registry.repository(record.id).counts()}


def get_project(app: App, request: Request, pid: str) -> Dict[str, Any]:
    record, repository = _project(app, pid)
    return {"project": record.to_dict(), "counts": repository.counts()}


def update_project(app: App, request: Request, pid: str) -> Dict[str, Any]:
    payload = request.json()
    record = app.registry.update(
        pid,
        name=payload.get("name"),
        description=payload.get("description"),
        base_url=payload.get("base_url"),
    )
    return {"project": record.to_dict()}


def delete_project(app: App, request: Request, pid: str) -> Dict[str, Any]:
    if request.param("confirm", "") != pid:
        raise ValidationError("删除项目需要确认参数 confirm=%s" % pid)
    app.registry.delete(pid)
    return {"deleted": True, "project_id": pid}


def project_stats(app: App, request: Request, pid: str) -> Dict[str, Any]:
    _, repository = _project(app, pid)
    recent = request.int_param("recent", 10, minimum=1, maximum=100)
    return repository.stats(recent=recent)


# --------------------------------------------------------------------- 接口
def list_endpoints(app: App, request: Request, pid: str) -> Dict[str, Any]:
    _, repository = _project(app, pid)
    page = _paginate(request)
    items, total = repository.list_endpoints(
        folder=request.param("folder", ""),
        q=request.param("q", ""),
        method=request.param("method", ""),
        tag=request.param("tag", ""),
        limit=page["limit"],
        offset=page["offset"],
    )
    return _list_payload([item.to_dict() for item in items], total, page["limit"], page["offset"])


def create_endpoint(app: App, request: Request, pid: str) -> Dict[str, Any]:
    _, repository = _project(app, pid)
    endpoint = EndpointSpec.from_dict(_spec(request.json()))
    if not endpoint.name:
        endpoint.name = "%s %s" % (endpoint.method, endpoint.path)
    endpoint.source = endpoint.source or "manual"
    saved = repository.create_endpoint(endpoint)
    return {"endpoint": saved.to_dict()}


def get_endpoint(app: App, request: Request, pid: str, eid: str) -> Dict[str, Any]:
    _, repository = _project(app, pid)
    return {"endpoint": repository.get_endpoint(int(eid)).to_dict()}


def update_endpoint(app: App, request: Request, pid: str, eid: str) -> Dict[str, Any]:
    _, repository = _project(app, pid)
    endpoint = EndpointSpec.from_dict(_spec(request.json()))
    saved = repository.update_endpoint(int(eid), endpoint)
    return {"endpoint": saved.to_dict()}


def delete_endpoint(app: App, request: Request, pid: str, eid: str) -> Dict[str, Any]:
    _, repository = _project(app, pid)
    repository.delete_endpoint(int(eid))
    return {"deleted": True, "endpoint_id": int(eid)}


def endpoint_to_case(app: App, request: Request, pid: str, eid: str) -> Dict[str, Any]:
    from ..assertions import default_assertions_for

    _, repository = _project(app, pid)
    endpoint = repository.get_endpoint(int(eid))
    payload = request.json()
    case = CaseSpec(
        name=str(payload.get("name") or "%s - 冒烟" % (endpoint.name or endpoint.path)),
        folder=endpoint.folder,
        description="由接口 %s 自动生成" % endpoint.signature(),
        endpoint_id=endpoint.id,
        request=RequestSpec.from_dict(endpoint.request.to_dict()),
        assertions=(
            default_assertions_for(endpoint.method)
            if payload.get("generate_assertions", True)
            else []
        ),
        tags=list(endpoint.tags),
        source=endpoint.source if endpoint.source in CASE_SOURCES else "manual",
        source_ref=endpoint.signature(),
    )
    saved = repository.create_case(case)
    return {"case": saved.to_dict()}


def probe_endpoint(app: App, request: Request, pid: str, eid: str) -> Dict[str, Any]:
    from ..http_client import request as http_request
    from ..variables import build_resolver

    record, repository = _project(app, pid)
    endpoint = repository.get_endpoint(int(eid))
    payload = request.json()
    environment = repository.resolve_environment(payload.get("env_id"))
    base_url = endpoint.request.base_url or environment.base_url or record.base_url
    if not base_url:
        raise ValidationError("未配置 base_url：请在接口、环境或项目中设置请求地址")
    resolver = build_resolver(dict(payload.get("variables") or {}), dict(environment.variables or {}))
    response = http_request(endpoint.request, app.config, base_url=base_url, resolver=resolver)
    return {"response": response.to_dict(), "base_url": base_url}


def folders(app: App, request: Request, pid: str) -> Dict[str, Any]:
    _, repository = _project(app, pid)
    return repository.folder_summary()


# --------------------------------------------------------------------- 用例
def list_cases(app: App, request: Request, pid: str) -> Dict[str, Any]:
    _, repository = _project(app, pid)
    page = _paginate(request)
    enabled_raw = request.param("enabled", "")
    enabled = None if enabled_raw == "" else enabled_raw.lower() in ("1", "true", "yes")
    items, total = repository.list_cases(
        folder=request.param("folder", ""),
        q=request.param("q", ""),
        tag=request.param("tag", ""),
        enabled=enabled,
        priority=request.param("priority", ""),
        limit=page["limit"],
        offset=page["offset"],
    )
    return _list_payload([item.to_dict() for item in items], total, page["limit"], page["offset"])


def create_case(app: App, request: Request, pid: str) -> Dict[str, Any]:
    _, repository = _project(app, pid)
    case = CaseSpec.from_dict(_spec(request.json()))
    saved = repository.create_case(case)
    return {"case": saved.to_dict()}


def get_case(app: App, request: Request, pid: str, cid: str) -> Dict[str, Any]:
    _, repository = _project(app, pid)
    return {"case": repository.get_case(int(cid)).to_dict()}


def update_case(app: App, request: Request, pid: str, cid: str) -> Dict[str, Any]:
    _, repository = _project(app, pid)
    case = CaseSpec.from_dict(_spec(request.json()))
    saved = repository.update_case(int(cid), case)
    return {"case": saved.to_dict()}


def delete_case(app: App, request: Request, pid: str, cid: str) -> Dict[str, Any]:
    _, repository = _project(app, pid)
    repository.delete_case(int(cid))
    return {"deleted": True, "case_id": int(cid)}


def duplicate_case(app: App, request: Request, pid: str, cid: str) -> Dict[str, Any]:
    _, repository = _project(app, pid)
    source = repository.get_case(int(cid))
    clone = CaseSpec.from_dict(source.to_dict())
    clone.id = None
    clone.name = "%s - 副本" % source.name
    clone.created_at = ""
    clone.updated_at = ""
    saved = repository.create_case(clone)
    return {"case": saved.to_dict()}


def run_case_now(app: App, request: Request, pid: str, cid: str) -> Dict[str, Any]:
    """同步执行单条用例（调试用），结果同样进入运行历史。"""
    from ..runner import CaseRunner, RunRequest

    record, repository = _project(app, pid)
    case = repository.get_case(int(cid))
    payload = request.json()
    environment = repository.resolve_environment(payload.get("env_id"))
    variables = dict(payload.get("variables") or {})
    if payload.get("persist", True):
        run_request = RunRequest(
            project_id=pid,
            case_ids=[int(cid)],
            env_id=environment.id,
            variables=variables,
            trigger="manual",
            note="单条用例调试",
            operator=str(payload.get("operator", "") or ""),
        )
        run = app.runs.run_sync(run_request, timeout_s=float(payload.get("timeout_s") or 120))
        result = run.case_results[0] if run.case_results else None
        return {"result": (result.to_dict() if result else None), "run": run.to_dict(with_cases=False)}
    runner = CaseRunner(app.config, repository, record)
    result = runner.run_case(case, environment, variables)
    return {"result": result.to_dict()}


# --------------------------------------------------------------------- 套件
def list_suites(app: App, request: Request, pid: str) -> Dict[str, Any]:
    _, repository = _project(app, pid)
    page = _paginate(request)
    suites, total = repository.list_suites(limit=page["limit"], offset=page["offset"])
    items = []
    for suite in suites:
        payload = suite.to_dict()
        payload["case_count"] = len(suite.case_ids)
        items.append(payload)
    return _list_payload(items, total, page["limit"], page["offset"])


def create_suite(app: App, request: Request, pid: str) -> Dict[str, Any]:
    _, repository = _project(app, pid)
    suite = SuiteSpec.from_dict(_spec(request.json()))
    if not suite.name:
        raise ValidationError("套件名称不能为空")
    saved = repository.create_suite(suite)
    return {"suite": saved.to_dict()}


def get_suite(app: App, request: Request, pid: str, sid: str) -> Dict[str, Any]:
    _, repository = _project(app, pid)
    return {"suite": repository.get_suite(int(sid)).to_dict()}


def update_suite(app: App, request: Request, pid: str, sid: str) -> Dict[str, Any]:
    _, repository = _project(app, pid)
    suite = SuiteSpec.from_dict(_spec(request.json()))
    saved = repository.update_suite(int(sid), suite)
    return {"suite": saved.to_dict()}


def delete_suite(app: App, request: Request, pid: str, sid: str) -> Dict[str, Any]:
    _, repository = _project(app, pid)
    repository.delete_suite(int(sid))
    return {"deleted": True, "suite_id": int(sid)}


# --------------------------------------------------------------------- 环境
def list_environments(app: App, request: Request, pid: str) -> Dict[str, Any]:
    _, repository = _project(app, pid)
    items = [item.to_dict() for item in repository.list_environments()]
    return {"items": items, "total": len(items)}


def create_environment(app: App, request: Request, pid: str) -> Dict[str, Any]:
    _, repository = _project(app, pid)
    environment = EnvironmentSpec.from_dict(_spec(request.json()))
    if not environment.name:
        raise ValidationError("环境名称不能为空")
    saved = repository.create_environment(environment)
    return {"environment": saved.to_dict()}


def get_environment(app: App, request: Request, pid: str, eid: str) -> Dict[str, Any]:
    _, repository = _project(app, pid)
    return {"environment": repository.get_environment(int(eid)).to_dict()}


def update_environment(app: App, request: Request, pid: str, eid: str) -> Dict[str, Any]:
    _, repository = _project(app, pid)
    environment = EnvironmentSpec.from_dict(_spec(request.json()))
    saved = repository.update_environment(int(eid), environment)
    return {"environment": saved.to_dict()}


def delete_environment(app: App, request: Request, pid: str, eid: str) -> Dict[str, Any]:
    _, repository = _project(app, pid)
    repository.delete_environment(int(eid))
    return {"deleted": True, "environment_id": int(eid)}


def set_default_environment(app: App, request: Request, pid: str, eid: str) -> Dict[str, Any]:
    _, repository = _project(app, pid)
    saved = repository.set_default_environment(int(eid))
    return {"environment": saved.to_dict()}


# --------------------------------------------------------------------- 运行
def create_run(app: App, request: Request, pid: str) -> Dict[str, Any]:
    from ..runner import RunRequest

    payload = request.json()
    case_ids = [int(item) for item in (payload.get("case_ids") or [])]
    suite_id = payload.get("suite_id")
    if suite_id is not None:
        suite_id = int(suite_id)
    if not case_ids and suite_id is None:
        raise ValidationError("必须提供 suite_id 或 case_ids")
    run_request = RunRequest(
        project_id=pid,
        case_ids=case_ids,
        suite_id=suite_id,
        env_id=(int(payload["env_id"]) if payload.get("env_id") is not None else None),
        trigger=str(payload.get("trigger", "manual") or "manual"),
        note=str(payload.get("note", "") or ""),
        operator=str(payload.get("operator", "") or ""),
        request_id=str(payload.get("request_id", "") or ""),
        variables=dict(payload.get("variables") or {}),
    )
    if run_request.request_id:
        existing = app.runs.find_existing(run_request)
        if existing is not None:
            return {"run": existing.to_dict(with_cases=False), "idempotent": True}
    run = app.runs.submit(run_request)
    background = bool(payload.get("background", True))
    if not background:
        finished = app.runs.wait(run.project_id, run.id, timeout_s=float(payload.get("timeout_s") or 600))
        return {"run": finished.to_dict(with_cases=True), "idempotent": False}
    return {"run": run.to_dict(with_cases=False), "idempotent": False}


def list_runs(app: App, request: Request, pid: str) -> Dict[str, Any]:
    _, repository = _project(app, pid)
    page = _paginate(request)
    suite_id = request.param("suite_id", "")
    runs, total = repository.list_runs(
        status=request.param("status", ""),
        suite_id=(int(suite_id) if suite_id else None),
        limit=page["limit"],
        offset=page["offset"],
    )
    return _list_payload([item.to_dict(with_cases=False) for item in runs], total, page["limit"], page["offset"])


def get_run(app: App, request: Request, pid: str, rid: str) -> Dict[str, Any]:
    _, repository = _project(app, pid)
    return {"run": repository.get_run(rid, with_cases=True).to_dict(with_cases=True)}


def cancel_run(app: App, request: Request, pid: str, rid: str) -> Dict[str, Any]:
    canceled = app.runs.cancel(pid, rid)
    return {"canceled": canceled, "run_id": rid}


def run_report(app: App, request: Request, pid: str, rid: str) -> Response:
    from ..report import to_html_report, to_json_report, to_junit_xml

    record, repository = _project(app, pid)
    run = repository.get_run(rid, with_cases=True)
    fmt = request.param("format", "html").lower()
    if fmt == "xml":
        body = to_junit_xml(run)
        return Response(
            status=200,
            body=body.encode("utf-8"),
            content_type="application/xml; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="run-%s.xml"' % run.id},
        )
    if fmt == "json":
        return Response(
            status=200,
            body=to_json_report(run).encode("utf-8"),
            content_type="application/json; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="run-%s.json"' % run.id},
        )
    html = to_html_report(run, project_name=record.name, title="%s · %s" % (record.name, run.id))
    return Response(status=200, body=html.encode("utf-8"), content_type="text/html; charset=utf-8")


def run_diff(app: App, request: Request, pid: str, rid: str) -> Dict[str, Any]:
    _, repository = _project(app, pid)
    after = repository.get_run(rid, with_cases=True)
    base_id = request.param("base", "")
    if base_id:
        before = repository.get_run(base_id, with_cases=True)
    else:
        runs, _ = repository.list_runs(limit=50)
        candidates = [item for item in runs if item.id != rid and item.suite_id == after.suite_id]
        if not candidates:
            candidates = [item for item in runs if item.id != rid]
        if not candidates:
            raise NotFoundError("没有可对比的基线运行")
        before = repository.get_run(candidates[0].id, with_cases=True)
    return diff_runs(before, after)


def compare_runs(app: App, request: Request, pid: str) -> Dict[str, Any]:
    _, repository = _project(app, pid)
    before_id = request.param("before", "")
    after_id = request.param("after", "")
    if not before_id or not after_id:
        raise ValidationError("必须提供 before 与 after")
    return diff_runs(repository.get_run(before_id, with_cases=True), repository.get_run(after_id, with_cases=True))


# --------------------------------------------------------------------- 导入
def list_imports(app: App, request: Request, pid: str) -> Dict[str, Any]:
    _, repository = _project(app, pid)
    items = repository.list_imports(limit=request.int_param("limit", 50, minimum=1, maximum=200))
    return {"items": items, "total": len(items)}


def _import_payload(app: App, request: Request) -> Any:
    payload = request.json()
    return load_import(
        app.config,
        source=str(payload.get("source", "auto") or "auto"),
        text=payload.get("text"),
        path=payload.get("path"),
        options=payload.get("options"),
    )


def preview_import(app: App, request: Request, pid: str) -> Dict[str, Any]:
    _project(app, pid)
    result = _import_payload(app, request)
    data = result.to_dict()
    limit = 200
    truncated = len(data["endpoints"]) > limit
    data["endpoints"] = data["endpoints"][:limit]
    data["cases"] = data["cases"][:limit]
    data["truncated"] = truncated
    data["detected"] = detected_hints(result)
    data["stats"]["warnings"] = list(data["stats"].get("warnings") or []) + quality_warnings(result)
    return data


def commit_import(app: App, request: Request, pid: str) -> Dict[str, Any]:
    record, repository = _project(app, pid)
    payload = request.json()
    result = _import_payload(app, request)
    mode = str(payload.get("mode", "merge") or "merge")
    stats = apply_import(repository, result, mode=mode)
    hints = detected_hints(result)
    discovered = hints.get("detected_server_url", "")
    if discovered and not repository.resolve_environment(None).base_url:
        seed_environment_base_url(repository, result, discovered)
    batch_id = repository.record_import(stats, title=result.title or record.name)
    return {"stats": stats.to_dict(), "batch_id": batch_id, "detected": hints, "source": result.source}


def fetch_import(app: App, request: Request, pid: str) -> Dict[str, Any]:
    _project(app, pid)
    payload = request.json()
    url = str(payload.get("url", "") or "")
    headers = {str(k): str(v) for k, v in dict(payload.get("headers") or {}).items()}
    text, final_url, size = fetch_spec(app.config, url, headers=headers)
    result = load_import(app.config, source=str(payload.get("source", "auto") or "auto"), text=text, options=payload.get("options"))
    data = result.to_dict()
    limit = 200
    data["truncated"] = len(data["endpoints"]) > limit
    data["endpoints"] = data["endpoints"][:limit]
    data["cases"] = data["cases"][:limit]
    data["detected"] = detected_hints(result)
    return {"preview": data, "url": final_url, "bytes": size, "text_preview": text[:2000]}


def mcp_import(app: App, request: Request, pid: str) -> Dict[str, Any]:
    """通过 MCP 从 Apifox 同步接口（预览或直接入库）。

    访问令牌只从服务端环境变量或 .env.local 读取，**不接受浏览器传入**；
    这样令牌不会出现在界面、运行记录或请求日志里。
    """
    from ..sync import load_import_from_mcp

    record, repository = _project(app, pid)
    payload = request.json()
    project_id = str(payload.get("apifox_project_id") or payload.get("project_id") or "").strip()
    doc_id = str(payload.get("doc_id") or "").strip()
    spec_file = str(payload.get("spec_file") or "").strip()
    if not (project_id or doc_id or spec_file):
        raise ValidationError("请提供 Apifox 项目 ID（apifox_project_id）")
    command = payload.get("command")
    command_parts = shlex.split(command) if isinstance(command, str) and command.strip() else None
    options = payload.get("options")
    mode = str(payload.get("mode", "merge") or "merge")
    preview = bool(payload.get("preview", False))
    token_env = str(payload.get("token_env", "APIFOX_ACCESS_TOKEN") or "APIFOX_ACCESS_TOKEN")

    result, meta = load_import_from_mcp(
        app.config,
        project_id=project_id or None,
        doc_id=doc_id or None,
        spec_file=spec_file or None,
        command=command_parts,
        token_env=token_env,
        refresh=bool(payload.get("refresh", True)),
        timeout_s=float(payload.get("timeout_s") or 180.0),
        api_base_url=(str(payload["api_base_url"]) if payload.get("api_base_url") else None),
        branch_id=(str(payload["branch_id"]) if payload.get("branch_id") else None),
        options=options,
    )
    hints = detected_hints(result)
    discovered = hints.get("detected_server_url", "")
    if discovered and not repository.resolve_environment(None).base_url:
        seed_environment_base_url(repository, result, discovered)

    if preview:
        data = result.to_dict()
        limit = 200
        data["truncated"] = len(data["endpoints"]) > limit
        data["endpoints"] = data["endpoints"][:limit]
        data["cases"] = data["cases"][:limit]
        data["detected"] = hints
        data["stats"]["warnings"] = list(data["stats"].get("warnings") or []) + quality_warnings(result)
        return {"preview": data, "mcp": meta, "mode": mode}

    stats = apply_import(repository, result, mode=mode)
    batch_id = repository.record_import(stats, title=result.title or record.name)
    return {"stats": stats.to_dict(), "batch_id": batch_id, "detected": hints, "mcp": meta, "source": "apifox"}


def export_project(app: App, request: Request, pid: str) -> Dict[str, Any]:
    _, repository = _project(app, pid)
    reveal = request.param("reveal", "0") in ("1", "true", "yes")
    return repository.export_all(reveal_secrets=reveal)


# --------------------------------------------------------------------- 注册
def register(router: Router) -> None:
    r = router.add
    r("GET", r"/api/v1/health", health)
    r("GET", r"/api/v1/meta", meta)

    r("GET", r"/api/v1/projects", list_projects)
    r("POST", r"/api/v1/projects", create_project)
    r("GET", r"/api/v1/projects/%s" % PID, get_project)
    r("PUT", r"/api/v1/projects/%s" % PID, update_project)
    r("DELETE", r"/api/v1/projects/%s" % PID, delete_project)
    r("GET", r"/api/v1/projects/%s/stats" % PID, project_stats)

    r("GET", r"/api/v1/projects/%s/endpoints" % PID, list_endpoints)
    r("POST", r"/api/v1/projects/%s/endpoints" % PID, create_endpoint)
    r("GET", r"/api/v1/projects/%s/endpoints/(?P<eid>\d+)" % PID, get_endpoint)
    r("PUT", r"/api/v1/projects/%s/endpoints/(?P<eid>\d+)" % PID, update_endpoint)
    r("DELETE", r"/api/v1/projects/%s/endpoints/(?P<eid>\d+)" % PID, delete_endpoint)
    r("POST", r"/api/v1/projects/%s/endpoints/(?P<eid>\d+)/to-case" % PID, endpoint_to_case)
    r("POST", r"/api/v1/projects/%s/endpoints/(?P<eid>\d+)/probe" % PID, probe_endpoint)
    r("GET", r"/api/v1/projects/%s/folders" % PID, folders)

    r("GET", r"/api/v1/projects/%s/cases" % PID, list_cases)
    r("POST", r"/api/v1/projects/%s/cases" % PID, create_case)
    r("GET", r"/api/v1/projects/%s/cases/(?P<cid>\d+)" % PID, get_case)
    r("PUT", r"/api/v1/projects/%s/cases/(?P<cid>\d+)" % PID, update_case)
    r("DELETE", r"/api/v1/projects/%s/cases/(?P<cid>\d+)" % PID, delete_case)
    r("POST", r"/api/v1/projects/%s/cases/(?P<cid>\d+)/duplicate" % PID, duplicate_case)
    r("POST", r"/api/v1/projects/%s/cases/(?P<cid>\d+)/run" % PID, run_case_now)

    r("GET", r"/api/v1/projects/%s/suites" % PID, list_suites)
    r("POST", r"/api/v1/projects/%s/suites" % PID, create_suite)
    r("GET", r"/api/v1/projects/%s/suites/(?P<sid>\d+)" % PID, get_suite)
    r("PUT", r"/api/v1/projects/%s/suites/(?P<sid>\d+)" % PID, update_suite)
    r("DELETE", r"/api/v1/projects/%s/suites/(?P<sid>\d+)" % PID, delete_suite)

    r("GET", r"/api/v1/projects/%s/environments" % PID, list_environments)
    r("POST", r"/api/v1/projects/%s/environments" % PID, create_environment)
    r("GET", r"/api/v1/projects/%s/environments/(?P<eid>\d+)" % PID, get_environment)
    r("PUT", r"/api/v1/projects/%s/environments/(?P<eid>\d+)" % PID, update_environment)
    r("DELETE", r"/api/v1/projects/%s/environments/(?P<eid>\d+)" % PID, delete_environment)
    r("POST", r"/api/v1/projects/%s/environments/(?P<eid>\d+)/default" % PID, set_default_environment)

    r("GET", r"/api/v1/projects/%s/runs" % PID, list_runs)
    r("POST", r"/api/v1/projects/%s/runs" % PID, create_run)
    r("GET", r"/api/v1/projects/%s/runs/compare" % PID, compare_runs)
    r("GET", r"/api/v1/projects/%s/runs/%s" % (PID, RID), get_run)
    r("POST", r"/api/v1/projects/%s/runs/%s/cancel" % (PID, RID), cancel_run)
    r("GET", r"/api/v1/projects/%s/runs/%s/report" % (PID, RID), run_report)
    r("GET", r"/api/v1/projects/%s/runs/%s/diff" % (PID, RID), run_diff)

    r("GET", r"/api/v1/projects/%s/imports" % PID, list_imports)
    r("POST", r"/api/v1/projects/%s/imports/preview" % PID, preview_import)
    r("POST", r"/api/v1/projects/%s/imports" % PID, commit_import)
    r("POST", r"/api/v1/projects/%s/imports/fetch" % PID, fetch_import)
    r("POST", r"/api/v1/projects/%s/imports/mcp" % PID, mcp_import)

    r("GET", r"/api/v1/projects/%s/export" % PID, export_project)
