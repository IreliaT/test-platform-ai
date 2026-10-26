"""项目注册表与项目级仓储。

- :class:`ProjectRegistry` 管理平台级项目清单（``data/registry.json``）与每个项目的数据库连接；
- :class:`Repository` 是单个项目内的数据访问层，所有查询都以项目数据库为边界，
  不同项目之间不可能互相读到数据（隔离由「一项目一 DB 文件」保证）。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import db
from .config import PlatformConfig, safe_project_id, slugify
from .errors import ConflictError, NotFoundError, ValidationError
from .models import (
    AssertionSpec,
    CaseResult,
    CaseSpec,
    EndpointSpec,
    EnvironmentSpec,
    ImportStats,
    RunResult,
    SuiteSpec,
    now_iso,
    new_id,
)
from .redact import MASK

REGISTRY_SCHEMA_VERSION = 1


def _sync_endpoint_request(endpoint: EndpointSpec) -> None:
    """保持不变量：endpoint.method/path 与 request 模板中的 method/path 一致。"""
    endpoint.request.method = endpoint.method
    endpoint.request.path = endpoint.path


def path_template_matches(template: str, path: str) -> bool:
    """判断具体路径是否匹配接口模板（``{id``}`` / ``:id`` 段视为通配）。"""
    from .models import normalize_path

    template_segments = normalize_path(template).split("/")
    path_segments = normalize_path(path).split("/")
    if len(template_segments) != len(path_segments):
        return False
    for expected, actual in zip(template_segments, path_segments):
        if expected == actual:
            continue
        if (expected.startswith("{") and expected.endswith("}")) or expected.startswith(":"):
            if not actual:
                return False
            continue
        return False
    return True


@dataclass
class ProjectRecord:
    id: str
    name: str
    description: str = ""
    base_url: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "base_url": self.base_url,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProjectRecord":
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "") or data.get("id", "")),
            description=str(data.get("description", "") or ""),
            base_url=str(data.get("base_url", "") or ""),
            created_at=str(data.get("created_at", "") or ""),
            updated_at=str(data.get("updated_at", "") or ""),
        )


class ProjectRegistry:
    """平台级项目清单 + 每个项目的数据库连接缓存。"""

    def __init__(self, config: PlatformConfig) -> None:
        self.config = config
        self.config.ensure_dirs()
        self._lock = threading.RLock()
        self._connections: List[sqlite3.Connection] = []
        self._repos: Dict[str, "Repository"] = {}
        self._local = threading.local()

    # --- 注册表文件 ---
    def _read_registry(self) -> Dict[str, Any]:
        path = self.config.registry_path
        if not path.exists():
            return {"schema_version": REGISTRY_SCHEMA_VERSION, "projects": {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {"schema_version": REGISTRY_SCHEMA_VERSION, "projects": {}}
        if not isinstance(data, dict):
            return {"schema_version": REGISTRY_SCHEMA_VERSION, "projects": {}}
        data.setdefault("projects", {})
        return data

    def _write_registry(self, data: Dict[str, Any]) -> None:
        path = self.config.registry_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    # --- 项目 CRUD ---
    def list_projects(self) -> List[ProjectRecord]:
        with self._lock:
            data = self._read_registry()
            records = [ProjectRecord.from_dict(item) for item in data["projects"].values()]
        records.sort(key=lambda item: item.created_at or item.id)
        return records

    def exists(self, project_id: str) -> bool:
        with self._lock:
            data = self._read_registry()
            return safe_project_id(project_id) in data["projects"]

    def get(self, project_id: str) -> ProjectRecord:
        pid = safe_project_id(project_id)
        with self._lock:
            data = self._read_registry()
            raw = data["projects"].get(pid)
        if raw is None:
            raise NotFoundError("项目不存在: %s" % pid)
        return ProjectRecord.from_dict(raw)

    def create(
        self,
        name: str,
        project_id: Optional[str] = None,
        description: str = "",
        base_url: str = "",
    ) -> ProjectRecord:
        if not name or not name.strip():
            raise ValidationError("项目名称不能为空")
        # 显式指定的项目 ID 必须严格合法；由名称推导时才做 slug 化
        pid = safe_project_id(project_id) if project_id else slugify(name)
        with self._lock:
            data = self._read_registry()
            if pid in data["projects"]:
                raise ConflictError("项目已存在: %s" % pid)
            record = ProjectRecord(
                id=pid,
                name=name.strip(),
                description=description or "",
                base_url=(base_url or "").strip(),
                created_at=now_iso(),
                updated_at=now_iso(),
            )
            data["projects"][pid] = record.to_dict()
            self._write_registry(data)
            self.config.project_dir(pid).mkdir(parents=True, exist_ok=True)
            environment = EnvironmentSpec(
                name="默认环境",
                base_url=record.base_url,
                variables={},
                is_default=True,
                created_at=now_iso(),
                updated_at=now_iso(),
            )
            self.repository(pid).create_environment(environment)
        return record

    def update(self, project_id: str, **fields: Any) -> ProjectRecord:
        pid = safe_project_id(project_id)
        with self._lock:
            data = self._read_registry()
            raw = data["projects"].get(pid)
            if raw is None:
                raise NotFoundError("项目不存在: %s" % pid)
            record = ProjectRecord.from_dict(raw)
            if "name" in fields and fields["name"]:
                record.name = str(fields["name"]).strip()
            if "description" in fields and fields["description"] is not None:
                record.description = str(fields["description"])
            if "base_url" in fields and fields["base_url"] is not None:
                record.base_url = str(fields["base_url"]).strip()
            record.updated_at = now_iso()
            data["projects"][pid] = record.to_dict()
            self._write_registry(data)
        return record

    def delete(self, project_id: str, purge: bool = True) -> None:
        pid = safe_project_id(project_id)
        with self._lock:
            data = self._read_registry()
            if pid not in data["projects"]:
                raise NotFoundError("项目不存在: %s" % pid)
            data["projects"].pop(pid)
            self._write_registry(data)
            self.close(pid)
            project_dir = self.config.project_dir(pid)
            if purge and project_dir.exists():
                # 先确认目录确实位于 data/projects 之下，再做删除
                projects_dir = self.config.projects_dir.resolve()
                target = project_dir.resolve()
                if projects_dir == target or projects_dir not in target.parents:
                    raise ValidationError("拒绝删除非项目目录: %s" % target)
                shutil.rmtree(target)

    # --- 连接与仓储 ---
    def connection(self, project_id: str) -> sqlite3.Connection:
        """返回**当前线程**的项目数据库连接。

        SQLite 连接不可跨线程共享（并发使用会崩溃），因此按线程缓存连接；
        不同线程各自持有连接，靠 WAL + busy_timeout 协调读写。
        """
        pid = safe_project_id(project_id)
        store = getattr(self._local, "connections", None)
        if store is None:
            store = {}
            self._local.connections = store
        conn = store.get(pid)
        if conn is None:
            if not self.exists(pid):
                raise NotFoundError("项目不存在: %s" % pid)
            conn = db.connect(self.config.project_db_path(pid))
            store[pid] = conn
            with self._lock:
                self._connections.append(conn)
        return conn

    def repository(self, project_id: str) -> "Repository":
        pid = safe_project_id(project_id)
        with self._lock:
            repo = self._repos.get(pid)
            if repo is None:
                repo = Repository(lambda: self.connection(pid), pid)
                self._repos[pid] = repo
            return repo

    def close(self, project_id: Optional[str] = None) -> None:
        """关闭连接。默认只关闭当前线程创建的连接，避免跨线程关闭他人连接。"""
        target = safe_project_id(project_id) if project_id else None
        store = getattr(self._local, "connections", {}) or {}
        for pid in list(store):
            if target is not None and pid != target:
                continue
            conn = store.pop(pid)
            if target is not None:
                self._repos.pop(pid, None)
            with self._lock:
                if conn in self._connections:
                    self._connections.remove(conn)
            try:
                conn.close()
            except sqlite3.Error:
                pass
        if target is None:
            with self._lock:
                self._repos.clear()


class Repository:
    """单项目数据访问层。所有写操作都会刷新 ``updated_at``。

    ``connection_provider`` 返回**当前线程**的连接；同一个 :class:`Repository`
    实例可以被多个线程复用（执行器与 Web 请求线程各自持有连接）。
    """

    def __init__(self, connection_provider: Any, project_id: str) -> None:
        self._connection_provider = connection_provider
        self.project_id = project_id
        self._lock = threading.RLock()

    @property
    def conn(self) -> sqlite3.Connection:
        return self._connection_provider()

    # ------------------------------------------------------------------ 接口
    def list_endpoints(
        self,
        folder: str = "",
        q: str = "",
        method: str = "",
        tag: str = "",
        limit: int = 200,
        offset: int = 0,
    ) -> Tuple[List[EndpointSpec], int]:
        where: List[str] = []
        params: List[Any] = []
        if folder:
            where.append("folder = ?")
            params.append(folder)
        if method:
            where.append("method = ?")
            params.append(method.upper())
        if q:
            where.append("(name LIKE ? OR path LIKE ? OR summary LIKE ? OR operation_id LIKE ?)")
            like = "%%%s%%" % q
            params.extend([like, like, like, like])
        if tag:
            where.append("tags_json LIKE ?")
            params.append('%%"%s"%%' % tag)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total_row = db.query_one(self.conn, "SELECT COUNT(*) AS c FROM endpoint" + clause, params)
        total = int(total_row["c"]) if total_row else 0
        rows = db.query_all(
            self.conn,
            "SELECT * FROM endpoint" + clause + " ORDER BY folder, path, method LIMIT ? OFFSET ?",
            params + [int(limit), int(offset)],
        )
        return [self._row_to_endpoint(row) for row in rows], total

    def get_endpoint(self, endpoint_id: int) -> EndpointSpec:
        row = db.query_one(self.conn, "SELECT * FROM endpoint WHERE id = ?", (int(endpoint_id),))
        if row is None:
            raise NotFoundError("接口不存在: %s" % endpoint_id)
        return self._row_to_endpoint(row)

    def find_endpoint_by_signature(self, method: str, path: str) -> Optional[EndpointSpec]:
        from .models import normalize_path

        signature = "%s %s" % (str(method).upper(), normalize_path(path))
        row = db.query_one(self.conn, "SELECT * FROM endpoint WHERE signature = ?", (signature,))
        return None if row is None else self._row_to_endpoint(row)

    def find_endpoint_for_path(self, method: str, path: str) -> Optional[EndpointSpec]:
        """按路径找接口：先精确匹配，再按路径模板匹配。

        生成用例时路径参数会被替换成示例值（``/api/users/{id}`` → ``/api/users/1``），
        只做精确匹配会让这类用例失去与接口的关联，因此需要模板回退。
        """
        exact = self.find_endpoint_by_signature(method, path)
        if exact is not None:
            return exact
        candidates, _ = self.list_endpoints(method=method, limit=5000)
        for endpoint in candidates:
            if path_template_matches(endpoint.path, path):
                return endpoint
        return None

    def create_endpoint(self, endpoint: EndpointSpec) -> EndpointSpec:
        from .models import normalize_path

        endpoint.path = normalize_path(endpoint.path)
        endpoint.method = endpoint.method.upper()
        _sync_endpoint_request(endpoint)
        signature = "%s %s" % (endpoint.method, endpoint.path)
        if db.query_one(self.conn, "SELECT id FROM endpoint WHERE signature = ?", (signature,)):
            raise ConflictError("接口已存在: %s" % signature)
        stamp = now_iso()
        endpoint.created_at = endpoint.created_at or stamp
        endpoint.updated_at = stamp
        with self._lock:
            cursor = db.execute(
                self.conn,
                """INSERT INTO endpoint
                   (name, folder, method, path, summary, description, tags_json, params_json, request_json,
                    source, source_ref, operation_id, deprecated, signature, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    endpoint.name or signature,
                    endpoint.folder,
                    endpoint.method,
                    endpoint.path,
                    endpoint.summary,
                    endpoint.description,
                    db.dumps(endpoint.tags),
                    db.dumps([item.to_dict() for item in endpoint.params]),
                    db.dumps(endpoint.request.to_dict()),
                    endpoint.source,
                    endpoint.source_ref,
                    endpoint.operation_id,
                    1 if endpoint.deprecated else 0,
                    signature,
                    endpoint.created_at,
                    endpoint.updated_at,
                ),
            )
        endpoint.id = int(cursor.lastrowid)
        return endpoint

    def update_endpoint(self, endpoint_id: int, endpoint: EndpointSpec) -> EndpointSpec:
        from .models import normalize_path

        current = self.get_endpoint(endpoint_id)
        endpoint.id = current.id
        endpoint.path = normalize_path(endpoint.path or current.path)
        endpoint.method = (endpoint.method or current.method).upper()
        _sync_endpoint_request(endpoint)
        endpoint.created_at = current.created_at
        endpoint.updated_at = now_iso()
        signature = "%s %s" % (endpoint.method, endpoint.path)
        clash = db.query_one(self.conn, "SELECT id FROM endpoint WHERE signature = ? AND id <> ?", (signature, current.id))
        if clash:
            raise ConflictError("接口已存在: %s" % signature)
        with self._lock:
            db.execute(
                self.conn,
                """UPDATE endpoint SET name=?, folder=?, method=?, path=?, summary=?, description=?,
                   tags_json=?, params_json=?, request_json=?, source=?, source_ref=?, operation_id=?,
                   deprecated=?, signature=?, updated_at=? WHERE id=?""",
                (
                    endpoint.name or signature,
                    endpoint.folder,
                    endpoint.method,
                    endpoint.path,
                    endpoint.summary,
                    endpoint.description,
                    db.dumps(endpoint.tags),
                    db.dumps([item.to_dict() for item in endpoint.params]),
                    db.dumps(endpoint.request.to_dict()),
                    endpoint.source,
                    endpoint.source_ref,
                    endpoint.operation_id,
                    1 if endpoint.deprecated else 0,
                    signature,
                    endpoint.updated_at,
                    current.id,
                ),
            )
        return endpoint

    def upsert_endpoint(self, endpoint: EndpointSpec) -> Tuple[EndpointSpec, str]:
        """按 method+path 幂等写入，返回 (endpoint, "created"|"updated"|"unchanged")。

        判定"是否变化"时忽略 ``source_ref``（它记录上一次同步来自哪份文档）。
        否则每次把契约导出成新文件名再同步，都会把全部接口算成"已更新"，
        让"这次同步到底改了什么"这个信号失效。出处仍然会被静默刷新。
        """
        from .models import normalize_path

        endpoint.path = normalize_path(endpoint.path)
        endpoint.method = endpoint.method.upper()
        _sync_endpoint_request(endpoint)
        existing = self.find_endpoint_by_signature(endpoint.method, endpoint.path)
        if existing is None:
            return self.create_endpoint(endpoint), "created"
        comparable_new = endpoint.to_dict()
        comparable_old = existing.to_dict()
        for key in ("id", "created_at", "updated_at", "source_ref"):
            comparable_new.pop(key, None)
            comparable_old.pop(key, None)
        if comparable_new == comparable_old:
            if existing.source_ref != endpoint.source_ref:
                with self._lock:
                    db.execute(
                        self.conn,
                        "UPDATE endpoint SET source_ref = ?, updated_at = ? WHERE id = ?",
                        (endpoint.source_ref, now_iso(), existing.id),
                    )
            endpoint.id = existing.id
            return existing, "unchanged"
        endpoint.id = existing.id
        return self.update_endpoint(existing.id, endpoint), "updated"

    def delete_endpoint(self, endpoint_id: int) -> None:
        self.get_endpoint(endpoint_id)
        with self._lock:
            db.execute(self.conn, "DELETE FROM endpoint WHERE id = ?", (int(endpoint_id),))
            db.execute(self.conn, "UPDATE test_case SET endpoint_id = NULL WHERE endpoint_id = ?", (int(endpoint_id),))

    def clear_endpoints(self) -> int:
        with self._lock:
            row = db.query_one(self.conn, "SELECT COUNT(*) AS c FROM endpoint")
            count = int(row["c"]) if row else 0
            db.execute(self.conn, "UPDATE test_case SET endpoint_id = NULL")
            db.execute(self.conn, "DELETE FROM endpoint")
        return count

    def folder_summary(self) -> Dict[str, List[str]]:
        endpoint_folders = [
            row["folder"] for row in db.query_all(self.conn, "SELECT DISTINCT folder FROM endpoint ORDER BY folder")
        ]
        case_folders = [
            row["folder"] for row in db.query_all(self.conn, "SELECT DISTINCT folder FROM test_case ORDER BY folder")
        ]
        suite_folders = [
            row["folder"] for row in db.query_all(self.conn, "SELECT DISTINCT folder FROM suite ORDER BY folder")
        ]
        tags: set = set()
        for row in db.query_all(self.conn, "SELECT tags_json FROM endpoint UNION ALL SELECT tags_json FROM test_case"):
            for tag in db.loads(row["tags_json"], []):
                tags.add(str(tag))
        return {
            "endpoint_folders": [item for item in endpoint_folders if item],
            "case_folders": [item for item in case_folders if item],
            "suite_folders": [item for item in suite_folders if item],
            "tags": sorted(tags),
        }

    def _row_to_endpoint(self, row: sqlite3.Row) -> EndpointSpec:
        from .models import ParamSpec, RequestSpec

        endpoint = EndpointSpec(
            id=int(row["id"]),
            name=row["name"],
            folder=row["folder"],
            method=row["method"],
            path=row["path"],
            summary=row["summary"],
            description=row["description"],
            tags=db.loads(row["tags_json"], []),
            params=[ParamSpec.from_dict(item) for item in db.loads(row["params_json"], [])],
            request=RequestSpec.from_dict(db.loads(row["request_json"], {})),
            source=row["source"],
            source_ref=row["source_ref"],
            operation_id=row["operation_id"],
            deprecated=bool(row["deprecated"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
        return endpoint

    # ------------------------------------------------------------------ 用例
    def list_cases(
        self,
        folder: str = "",
        q: str = "",
        tag: str = "",
        enabled: Optional[bool] = None,
        priority: str = "",
        endpoint_id: Optional[int] = None,
        limit: int = 500,
        offset: int = 0,
    ) -> Tuple[List[CaseSpec], int]:
        where: List[str] = []
        params: List[Any] = []
        if folder:
            where.append("folder = ?")
            params.append(folder)
        if q:
            where.append("(name LIKE ? OR description LIKE ?)")
            like = "%%%s%%" % q
            params.extend([like, like])
        if tag:
            where.append("tags_json LIKE ?")
            params.append('%%"%s"%%' % tag)
        if enabled is not None:
            where.append("enabled = ?")
            params.append(1 if enabled else 0)
        if priority:
            where.append("priority = ?")
            params.append(priority)
        if endpoint_id is not None:
            where.append("endpoint_id = ?")
            params.append(int(endpoint_id))
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total_row = db.query_one(self.conn, "SELECT COUNT(*) AS c FROM test_case" + clause, params)
        total = int(total_row["c"]) if total_row else 0
        rows = db.query_all(
            self.conn,
            "SELECT * FROM test_case" + clause + " ORDER BY folder, priority, id LIMIT ? OFFSET ?",
            params + [int(limit), int(offset)],
        )
        return [self._row_to_case(row) for row in rows], total

    def get_case(self, case_id: int) -> CaseSpec:
        row = db.query_one(self.conn, "SELECT * FROM test_case WHERE id = ?", (int(case_id),))
        if row is None:
            raise NotFoundError("用例不存在: %s" % case_id)
        return self._row_to_case(row)

    def get_cases(self, case_ids: Sequence[int]) -> List[CaseSpec]:
        if not case_ids:
            return []
        placeholders = ",".join("?" for _ in case_ids)
        rows = db.query_all(self.conn, "SELECT * FROM test_case WHERE id IN (%s)" % placeholders, list(case_ids))
        mapping = {int(row["id"]): self._row_to_case(row) for row in rows}
        return [mapping[cid] for cid in case_ids if cid in mapping]

    def create_case(self, case: CaseSpec) -> CaseSpec:
        self._validate_case(case)
        stamp = now_iso()
        case.created_at = case.created_at or stamp
        case.updated_at = stamp
        with self._lock:
            cursor = db.execute(
                self.conn,
                """INSERT INTO test_case
                   (name, folder, description, endpoint_id, request_json, assertions_json, extractors_json,
                    setup_json, variables_json, tags_json, priority, enabled, retries, source, source_ref,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    case.name,
                    case.folder,
                    case.description,
                    case.endpoint_id,
                    db.dumps(case.request.to_dict()),
                    db.dumps([item.to_dict() for item in case.assertions]),
                    db.dumps([item.to_dict() for item in case.extractors]),
                    db.dumps([item.to_dict() for item in case.setup]),
                    db.dumps(case.variables),
                    db.dumps(case.tags),
                    case.priority,
                    1 if case.enabled else 0,
                    int(case.retries or 0),
                    case.source,
                    case.source_ref,
                    case.created_at,
                    case.updated_at,
                ),
            )
        case.id = int(cursor.lastrowid)
        return case

    def update_case(self, case_id: int, case: CaseSpec) -> CaseSpec:
        current = self.get_case(case_id)
        self._validate_case(case)
        case.id = current.id
        case.created_at = current.created_at
        case.updated_at = now_iso()
        with self._lock:
            db.execute(
                self.conn,
                """UPDATE test_case SET name=?, folder=?, description=?, endpoint_id=?, request_json=?,
                   assertions_json=?, extractors_json=?, setup_json=?, variables_json=?, tags_json=?,
                   priority=?, enabled=?, retries=?, source=?, source_ref=?, updated_at=? WHERE id=?""",
                (
                    case.name,
                    case.folder,
                    case.description,
                    case.endpoint_id,
                    db.dumps(case.request.to_dict()),
                    db.dumps([item.to_dict() for item in case.assertions]),
                    db.dumps([item.to_dict() for item in case.extractors]),
                    db.dumps([item.to_dict() for item in case.setup]),
                    db.dumps(case.variables),
                    db.dumps(case.tags),
                    case.priority,
                    1 if case.enabled else 0,
                    int(case.retries or 0),
                    case.source,
                    case.source_ref,
                    case.updated_at,
                    current.id,
                ),
            )
        return case

    def delete_case(self, case_id: int) -> None:
        self.get_case(case_id)
        with self._lock:
            db.execute(self.conn, "DELETE FROM test_case WHERE id = ?", (int(case_id),))
            for row in db.query_all(self.conn, "SELECT id, case_ids_json FROM suite"):
                ids = db.loads(row["case_ids_json"], [])
                if int(case_id) in [int(item) for item in ids]:
                    remaining = [int(item) for item in ids if int(item) != int(case_id)]
                    db.execute(
                        self.conn,
                        "UPDATE suite SET case_ids_json = ?, updated_at = ? WHERE id = ?",
                        (db.dumps(remaining), now_iso(), row["id"]),
                    )

    def _validate_case(self, case: CaseSpec) -> None:
        from .models import validate_case

        errors = validate_case(case)
        if errors:
            raise ValidationError("用例校验失败", details=errors)

    def _row_to_case(self, row: sqlite3.Row) -> CaseSpec:
        from .models import ExtractorSpec, RequestSpec, StepSpec

        return CaseSpec(
            id=int(row["id"]),
            name=row["name"],
            folder=row["folder"],
            description=row["description"],
            endpoint_id=(None if row["endpoint_id"] is None else int(row["endpoint_id"])),
            request=RequestSpec.from_dict(db.loads(row["request_json"], {})),
            assertions=[AssertionSpec.from_dict(item) for item in db.loads(row["assertions_json"], [])],
            extractors=[ExtractorSpec.from_dict(item) for item in db.loads(row["extractors_json"], [])],
            setup=[StepSpec.from_dict(item) for item in db.loads(row["setup_json"], [])],
            variables=db.loads(row["variables_json"], {}),
            tags=db.loads(row["tags_json"], []),
            priority=row["priority"],
            enabled=bool(row["enabled"]),
            retries=int(row["retries"]),
            source=row["source"],
            source_ref=row["source_ref"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ------------------------------------------------------------------ 套件
    def list_suites(self, limit: int = 200, offset: int = 0) -> Tuple[List[SuiteSpec], int]:
        total_row = db.query_one(self.conn, "SELECT COUNT(*) AS c FROM suite")
        total = int(total_row["c"]) if total_row else 0
        rows = db.query_all(self.conn, "SELECT * FROM suite ORDER BY id LIMIT ? OFFSET ?", (int(limit), int(offset)))
        return [self._row_to_suite(row) for row in rows], total

    def get_suite(self, suite_id: int) -> SuiteSpec:
        row = db.query_one(self.conn, "SELECT * FROM suite WHERE id = ?", (int(suite_id),))
        if row is None:
            raise NotFoundError("套件不存在: %s" % suite_id)
        return self._row_to_suite(row)

    def create_suite(self, suite: SuiteSpec) -> SuiteSpec:
        stamp = now_iso()
        suite.created_at = suite.created_at or stamp
        suite.updated_at = stamp
        with self._lock:
            cursor = db.execute(
                self.conn,
                """INSERT INTO suite (name, folder, description, case_ids_json, env_id, variables_json,
                   failure_strategy, concurrency, tags_json, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    suite.name,
                    suite.folder,
                    suite.description,
                    db.dumps(suite.case_ids),
                    suite.env_id,
                    db.dumps(suite.variables or {}),
                    suite.failure_strategy,
                    int(suite.concurrency or 1),
                    db.dumps(suite.tags),
                    suite.created_at,
                    suite.updated_at,
                ),
            )
        suite.id = int(cursor.lastrowid)
        return suite

    def update_suite(self, suite_id: int, suite: SuiteSpec) -> SuiteSpec:
        current = self.get_suite(suite_id)
        suite.id = current.id
        suite.created_at = current.created_at
        suite.updated_at = now_iso()
        with self._lock:
            db.execute(
                self.conn,
                """UPDATE suite SET name=?, folder=?, description=?, case_ids_json=?, env_id=?, variables_json=?,
                   failure_strategy=?, concurrency=?, tags_json=?, updated_at=? WHERE id=?""",
                (
                    suite.name,
                    suite.folder,
                    suite.description,
                    db.dumps(suite.case_ids),
                    suite.env_id,
                    db.dumps(suite.variables or {}),
                    suite.failure_strategy,
                    int(suite.concurrency or 1),
                    db.dumps(suite.tags),
                    suite.updated_at,
                    current.id,
                ),
            )
        return suite

    def delete_suite(self, suite_id: int) -> None:
        self.get_suite(suite_id)
        with self._lock:
            db.execute(self.conn, "DELETE FROM suite WHERE id = ?", (int(suite_id),))

    def _row_to_suite(self, row: sqlite3.Row) -> SuiteSpec:
        return SuiteSpec(
            id=int(row["id"]),
            name=row["name"],
            # 只读连接可能跳过迁移，老库没有 folder 列时按未分组处理
            folder=(row["folder"] if "folder" in row.keys() else ""),
            description=row["description"],
            case_ids=[int(item) for item in db.loads(row["case_ids_json"], [])],
            env_id=(None if row["env_id"] is None else int(row["env_id"])),
            variables=db.loads(row["variables_json"], {}) if "variables_json" in row.keys() else {},
            failure_strategy=row["failure_strategy"],
            concurrency=int(row["concurrency"]),
            tags=db.loads(row["tags_json"], []),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ------------------------------------------------------------------ 环境
    def list_environments(self) -> List[EnvironmentSpec]:
        rows = db.query_all(self.conn, "SELECT * FROM environment ORDER BY is_default DESC, id")
        return [self._row_to_environment(row) for row in rows]

    def get_environment(self, env_id: int) -> EnvironmentSpec:
        row = db.query_one(self.conn, "SELECT * FROM environment WHERE id = ?", (int(env_id),))
        if row is None:
            raise NotFoundError("环境不存在: %s" % env_id)
        return self._row_to_environment(row)

    def default_environment(self) -> Optional[EnvironmentSpec]:
        row = db.query_one(self.conn, "SELECT * FROM environment ORDER BY is_default DESC, id LIMIT 1")
        return None if row is None else self._row_to_environment(row)

    def resolve_environment(self, env_id: Optional[int]) -> EnvironmentSpec:
        """按 id 取环境；未指定时取默认环境；都没有则返回空环境。"""
        if env_id is not None:
            return self.get_environment(env_id)
        found = self.default_environment()
        if found is not None:
            return found
        return EnvironmentSpec(name="未配置环境", base_url="", is_default=True)

    def create_environment(self, environment: EnvironmentSpec) -> EnvironmentSpec:
        stamp = now_iso()
        environment.created_at = environment.created_at or stamp
        environment.updated_at = stamp
        with self._lock:
            if environment.is_default:
                db.execute(self.conn, "UPDATE environment SET is_default = 0")
            cursor = db.execute(
                self.conn,
                """INSERT INTO environment (name, base_url, variables_json, auth_json, is_default,
                   created_at, updated_at) VALUES (?,?,?,?,?,?,?)""",
                (
                    environment.name,
                    environment.base_url,
                    db.dumps(environment.variables),
                    db.dumps(environment.auth.to_dict()),
                    1 if environment.is_default else 0,
                    environment.created_at,
                    environment.updated_at,
                ),
            )
        environment.id = int(cursor.lastrowid)
        return environment

    def update_environment(self, env_id: int, environment: EnvironmentSpec) -> EnvironmentSpec:
        current = self.get_environment(env_id)
        environment.id = current.id
        environment.created_at = current.created_at
        environment.updated_at = now_iso()
        # 前端回传的掩码值表示"保持原值"，不允许把 ****** 写库
        merged_vars = dict(current.variables)
        for key, value in (environment.variables or {}).items():
            if value == MASK and key in current.variables:
                merged_vars[key] = current.variables[key]
            else:
                merged_vars[key] = value
        environment.variables = merged_vars
        auth = environment.auth.to_dict()
        current_auth = current.auth.to_dict()
        for key in ("token", "password", "value"):
            if auth.get(key) == MASK and current_auth.get(key):
                auth[key] = current_auth[key]
        environment.auth = type(environment.auth).from_dict(auth)
        with self._lock:
            if environment.is_default:
                db.execute(self.conn, "UPDATE environment SET is_default = 0")
            db.execute(
                self.conn,
                """UPDATE environment SET name=?, base_url=?, variables_json=?, auth_json=?, is_default=?,
                   updated_at=? WHERE id=?""",
                (
                    environment.name,
                    environment.base_url,
                    db.dumps(environment.variables),
                    db.dumps(environment.auth.to_dict()),
                    1 if environment.is_default else 0,
                    environment.updated_at,
                    current.id,
                ),
            )
        return environment

    def delete_environment(self, env_id: int) -> None:
        self.get_environment(env_id)
        with self._lock:
            db.execute(self.conn, "DELETE FROM environment WHERE id = ?", (int(env_id),))

    def set_default_environment(self, env_id: int) -> EnvironmentSpec:
        environment = self.get_environment(env_id)
        with self._lock:
            db.execute(self.conn, "UPDATE environment SET is_default = 0")
            db.execute(
                self.conn,
                "UPDATE environment SET is_default = 1, updated_at = ? WHERE id = ?",
                (now_iso(), environment.id),
            )
        environment.is_default = True
        return environment

    def _row_to_environment(self, row: sqlite3.Row) -> EnvironmentSpec:
        from .models import AuthSpec

        return EnvironmentSpec(
            id=int(row["id"]),
            name=row["name"],
            base_url=row["base_url"],
            variables=db.loads(row["variables_json"], {}),
            auth=AuthSpec.from_dict(db.loads(row["auth_json"], {})),
            is_default=bool(row["is_default"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ------------------------------------------------------------------ 运行
    def create_run(self, run: RunResult) -> RunResult:
        run.id = run.id or new_id("run")
        with self._lock:
            db.execute(
                self.conn,
                """INSERT INTO run (id, status, trigger, suite_id, suite_name, env_id, env_name, total, passed,
                   failed, errored, skipped, duration_ms, started_at, finished_at, operator, note, request_id,
                   fingerprint, logs_json, case_ids_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run.id,
                    run.status,
                    run.trigger,
                    run.suite_id,
                    run.suite_name,
                    run.env_id,
                    run.env_name,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    run.started_at or now_iso(),
                    "",
                    run.operator,
                    run.note,
                    run.request_id,
                    run.fingerprint,
                    db.dumps(run.logs),
                    db.dumps([]),
                ),
            )
        return run

    def update_run(self, run: RunResult) -> None:
        run.recount()
        with self._lock:
            db.execute(
                self.conn,
                """UPDATE run SET status=?, suite_id=?, suite_name=?, env_id=?, env_name=?, total=?, passed=?,
                   failed=?, errored=?, incomplete=?, skipped=?, duration_ms=?, started_at=?, finished_at=?,
                   operator=?, note=?, request_id=?, fingerprint=?, logs_json=? WHERE id=?""",
                (
                    run.status,
                    run.suite_id,
                    run.suite_name,
                    run.env_id,
                    run.env_name,
                    run.total,
                    run.passed,
                    run.failed,
                    run.errored,
                    run.incomplete,
                    run.skipped,
                    run.duration_ms,
                    run.started_at,
                    run.finished_at,
                    run.operator,
                    run.note,
                    run.request_id,
                    run.fingerprint,
                    db.dumps(run.logs),
                    run.id,
                ),
            )
        self._ensure_counts_columns()

    def save_case_result(self, run_id: str, result: CaseResult) -> None:
        with self._lock:
            db.execute(
                self.conn,
                """INSERT INTO run_case_result (run_id, case_id, case_name, status, duration_ms, started_at,
                   attempts, request_json, response_json, assertions_json, extracted_json, steps_json, error,
                   logs_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    result.case_id,
                    result.case_name,
                    result.status,
                    int(result.duration_ms or 0),
                    result.started_at,
                    int(result.attempts or 0),
                    db.dumps(result.request),
                    db.dumps(result.response),
                    db.dumps([item.to_dict() for item in result.assertions]),
                    db.dumps(result.extracted),
                    db.dumps(result.steps),
                    result.error,
                    db.dumps(result.logs),
                ),
            )

    def _ensure_counts_columns(self) -> None:
        """兼容旧库：缺失 incomplete 列时补齐（v1 起就存在，保留幂等保护）。"""
        columns = {row["name"] for row in db.query_all(self.conn, "PRAGMA table_info(run)")}
        if "incomplete" not in columns:
            db.execute(self.conn, "ALTER TABLE run ADD COLUMN incomplete INTEGER NOT NULL DEFAULT 0")

    def list_runs(
        self,
        status: str = "",
        suite_id: Optional[int] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[RunResult], int]:
        where: List[str] = []
        params: List[Any] = []
        if status:
            where.append("status = ?")
            params.append(status)
        if suite_id is not None:
            where.append("suite_id = ?")
            params.append(int(suite_id))
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total_row = db.query_one(self.conn, "SELECT COUNT(*) AS c FROM run" + clause, params)
        total = int(total_row["c"]) if total_row else 0
        rows = db.query_all(
            self.conn,
            "SELECT * FROM run" + clause + " ORDER BY started_at DESC, rowid DESC LIMIT ? OFFSET ?",
            params + [int(limit), int(offset)],
        )
        return [self._row_to_run(row, with_cases=False) for row in rows], total

    def get_run(self, run_id: str, with_cases: bool = True) -> RunResult:
        row = db.query_one(self.conn, "SELECT * FROM run WHERE id = ?", (run_id,))
        if row is None:
            raise NotFoundError("运行记录不存在: %s" % run_id)
        return self._row_to_run(row, with_cases=with_cases)

    def find_run_by_request_id(self, request_id: str) -> Optional[RunResult]:
        if not request_id:
            return None
        row = db.query_one(
            self.conn, "SELECT * FROM run WHERE request_id = ? ORDER BY started_at DESC, rowid DESC LIMIT 1", (request_id,)
        )
        return None if row is None else self._row_to_run(row, with_cases=False)

    def set_run_status(self, run_id: str, status: str) -> None:
        with self._lock:
            db.execute(self.conn, "UPDATE run SET status = ? WHERE id = ?", (status, run_id))

    def delete_run(self, run_id: str) -> None:
        self.get_run(run_id, with_cases=False)
        with self._lock:
            db.execute(self.conn, "DELETE FROM run_case_result WHERE run_id = ?", (run_id,))
            db.execute(self.conn, "DELETE FROM run WHERE id = ?", (run_id,))

    def _row_to_run(self, row: sqlite3.Row, with_cases: bool = True) -> RunResult:
        columns = set(row.keys())
        run = RunResult(
            id=row["id"],
            project_id=self.project_id,
            status=row["status"],
            trigger=row["trigger"],
            suite_id=(None if row["suite_id"] is None else int(row["suite_id"])),
            suite_name=row["suite_name"],
            env_id=(None if row["env_id"] is None else int(row["env_id"])),
            env_name=row["env_name"],
            total=int(row["total"]),
            passed=int(row["passed"]),
            failed=int(row["failed"]),
            errored=int(row["errored"]),
            incomplete=int(row["incomplete"]) if "incomplete" in columns else 0,
            skipped=int(row["skipped"]),
            duration_ms=int(row["duration_ms"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            operator=row["operator"],
            note=row["note"],
            request_id=(row["request_id"] if "request_id" in columns else ""),
            fingerprint=(row["fingerprint"] if "fingerprint" in columns else ""),
        )
        if with_cases:
            run.logs = db.loads(row["logs_json"], [])
            case_rows = db.query_all(
                self.conn, "SELECT * FROM run_case_result WHERE run_id = ? ORDER BY id", (run.id,)
            )
            run.case_results = [self._row_to_case_result(item) for item in case_rows]
        return run

    def _row_to_case_result(self, row: sqlite3.Row) -> CaseResult:
        from .models import AssertionResult

        return CaseResult(
            case_id=(None if row["case_id"] is None else int(row["case_id"])),
            case_name=row["case_name"],
            status=row["status"],
            duration_ms=int(row["duration_ms"]),
            started_at=row["started_at"],
            attempts=int(row["attempts"]),
            request=db.loads(row["request_json"], {}),
            response=db.loads(row["response_json"], {}),
            assertions=[AssertionResult.from_dict(item) for item in db.loads(row["assertions_json"], [])],
            extracted=db.loads(row["extracted_json"], {}),
            steps=db.loads(row["steps_json"], []),
            error=row["error"],
            logs=db.loads(row["logs_json"], []),
        )

    # ------------------------------------------------------------------ 导入批次
    def record_import(self, stats: ImportStats, title: str = "") -> int:
        with self._lock:
            cursor = db.execute(
                self.conn,
                "INSERT INTO import_batch (source, source_ref, title, stats_json, created_at) VALUES (?,?,?,?,?)",
                (stats.source, stats.source_ref, title, db.dumps(stats.to_dict()), now_iso()),
            )
        return int(cursor.lastrowid)

    def list_imports(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = db.query_all(self.conn, "SELECT * FROM import_batch ORDER BY id DESC LIMIT ?", (int(limit),))
        return [
            {
                "id": int(row["id"]),
                "source": row["source"],
                "source_ref": row["source_ref"],
                "title": row["title"],
                "stats": db.loads(row["stats_json"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    # ------------------------------------------------------------------ 概览
    def counts(self) -> Dict[str, int]:
        return db.table_counts(self.conn)

    def stats(self, recent: int = 10) -> Dict[str, Any]:
        counts = self.counts()
        runs, total_runs = self.list_runs(limit=recent)
        finished = [run for run in runs if run.status in ("passed", "failed", "error", "incomplete")]
        passed = sum(1 for run in finished if run.status == "passed")
        errors_total = sum(run.errored for run in runs)
        failed_cases = sum(run.failed for run in runs)
        pass_rate = (passed / len(finished)) if finished else 0.0
        trend = [
            {
                "id": run.id,
                "started_at": run.started_at,
                "status": run.status,
                "total": run.total,
                "passed": run.passed,
                "failed": run.failed,
                "errored": run.errored,
                "incomplete": run.incomplete,
                "skipped": run.skipped,
                "duration_ms": run.duration_ms,
            }
            for run in reversed(runs)
        ]
        latest_case_status: Dict[str, int] = {}
        if runs:
            last = self.get_run(runs[0].id, with_cases=True)
            for result in last.case_results:
                latest_case_status[result.status] = latest_case_status.get(result.status, 0) + 1
        return {
            "counts": counts,
            "total_runs": total_runs,
            "pass_rate": round(pass_rate, 4),
            "recent_runs": [run.to_dict(with_cases=False) for run in runs],
            "last_run": (runs[0].to_dict(with_cases=False) if runs else None),
            "last_run_case_status": latest_case_status,
            "failed_cases_in_recent": failed_cases,
            "errors_in_recent": errors_total,
            "trend": trend,
        }

    def fingerprint(self, case_ids: Sequence[int]) -> str:
        """用例定义的指纹，用于历史对比时判断"跑的是不是同一份定义"。"""
        digest = hashlib.sha256()
        for case in self.get_cases(list(case_ids)):
            digest.update(db.dumps(case.to_dict()).encode("utf-8"))
        digest.update(self.project_id.encode("utf-8"))
        return digest.hexdigest()[:16]

    def export_all(self, reveal_secrets: bool = False) -> Dict[str, Any]:
        endpoints, _ = self.list_endpoints(limit=100000)
        cases, _ = self.list_cases(limit=100000)
        suites, _ = self.list_suites(limit=100000)
        return {
            "project_id": self.project_id,
            "exported_at": now_iso(),
            "endpoints": [item.to_dict() for item in endpoints],
            "cases": [item.to_dict() for item in cases],
            "suites": [item.to_dict() for item in suites],
            "environments": [item.to_dict(reveal_secrets=reveal_secrets) for item in self.list_environments()],
        }
