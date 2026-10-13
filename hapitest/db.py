"""SQLite 存储层。

隔离策略：每个项目一个独立数据库文件 ``data/projects/<id>/platform.db``，
项目之间的数据互不可见；平台级信息（项目清单）保存在 ``data/registry.json``。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS endpoint (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    folder        TEXT NOT NULL DEFAULT '',
    method        TEXT NOT NULL,
    path          TEXT NOT NULL,
    summary       TEXT NOT NULL DEFAULT '',
    description   TEXT NOT NULL DEFAULT '',
    tags_json     TEXT NOT NULL DEFAULT '[]',
    params_json   TEXT NOT NULL DEFAULT '[]',
    request_json  TEXT NOT NULL DEFAULT '{}',
    source        TEXT NOT NULL DEFAULT 'manual',
    source_ref    TEXT NOT NULL DEFAULT '',
    operation_id  TEXT NOT NULL DEFAULT '',
    deprecated    INTEGER NOT NULL DEFAULT 0,
    signature     TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_endpoint_signature ON endpoint(signature);
CREATE INDEX IF NOT EXISTS idx_endpoint_folder ON endpoint(folder);

CREATE TABLE IF NOT EXISTS test_case (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    folder        TEXT NOT NULL DEFAULT '',
    description   TEXT NOT NULL DEFAULT '',
    endpoint_id   INTEGER,
    request_json  TEXT NOT NULL DEFAULT '{}',
    assertions_json TEXT NOT NULL DEFAULT '[]',
    extractors_json TEXT NOT NULL DEFAULT '[]',
    setup_json    TEXT NOT NULL DEFAULT '[]',
    variables_json TEXT NOT NULL DEFAULT '{}',
    tags_json     TEXT NOT NULL DEFAULT '[]',
    priority      TEXT NOT NULL DEFAULT 'P2',
    enabled       INTEGER NOT NULL DEFAULT 1,
    retries       INTEGER NOT NULL DEFAULT 0,
    source        TEXT NOT NULL DEFAULT 'manual',
    source_ref    TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (endpoint_id) REFERENCES endpoint(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_case_endpoint ON test_case(endpoint_id);
CREATE INDEX IF NOT EXISTS idx_case_folder ON test_case(folder);

CREATE TABLE IF NOT EXISTS suite (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    case_ids_json TEXT NOT NULL DEFAULT '[]',
    env_id        INTEGER,
    failure_strategy TEXT NOT NULL DEFAULT 'continue',
    concurrency   INTEGER NOT NULL DEFAULT 1,
    tags_json     TEXT NOT NULL DEFAULT '[]',
    created_at    TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS environment (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    base_url      TEXT NOT NULL DEFAULT '',
    variables_json TEXT NOT NULL DEFAULT '{}',
    auth_json     TEXT NOT NULL DEFAULT '{}',
    is_default    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS run (
    id            TEXT PRIMARY KEY,
    status        TEXT NOT NULL DEFAULT 'queued',
    trigger       TEXT NOT NULL DEFAULT 'manual',
    suite_id      INTEGER,
    suite_name    TEXT NOT NULL DEFAULT '',
    env_id        INTEGER,
    env_name      TEXT NOT NULL DEFAULT '',
    total         INTEGER NOT NULL DEFAULT 0,
    passed        INTEGER NOT NULL DEFAULT 0,
    failed        INTEGER NOT NULL DEFAULT 0,
    errored       INTEGER NOT NULL DEFAULT 0,
    incomplete    INTEGER NOT NULL DEFAULT 0,
    skipped       INTEGER NOT NULL DEFAULT 0,
    duration_ms   INTEGER NOT NULL DEFAULT 0,
    started_at    TEXT NOT NULL DEFAULT '',
    finished_at   TEXT NOT NULL DEFAULT '',
    operator      TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT '',
    request_id    TEXT NOT NULL DEFAULT '',
    fingerprint   TEXT NOT NULL DEFAULT '',
    logs_json     TEXT NOT NULL DEFAULT '[]',
    case_ids_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_run_started ON run(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_run_request_id ON run(request_id);

CREATE TABLE IF NOT EXISTS run_case_result (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    case_id       INTEGER,
    case_name     TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'skipped',
    duration_ms   INTEGER NOT NULL DEFAULT 0,
    started_at    TEXT NOT NULL DEFAULT '',
    attempts      INTEGER NOT NULL DEFAULT 0,
    request_json  TEXT NOT NULL DEFAULT '{}',
    response_json TEXT NOT NULL DEFAULT '{}',
    assertions_json TEXT NOT NULL DEFAULT '[]',
    extracted_json TEXT NOT NULL DEFAULT '{}',
    steps_json    TEXT NOT NULL DEFAULT '[]',
    error         TEXT NOT NULL DEFAULT '',
    logs_json     TEXT NOT NULL DEFAULT '[]',
    FOREIGN KEY (run_id) REFERENCES run(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_result_run ON run_case_result(run_id);

CREATE TABLE IF NOT EXISTS import_batch (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,
    source_ref    TEXT NOT NULL DEFAULT '',
    title         TEXT NOT NULL DEFAULT '',
    stats_json    TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL DEFAULT ''
);
"""


def connect(db_path: Path, readonly: bool = False) -> sqlite3.Connection:
    """打开（并初始化）项目数据库。

    注意：``sqlite3.Connection`` **不是线程安全的**。平台为每个线程创建独立连接
    （见 :meth:`ProjectRegistry.connection`），绝不能跨线程共用一个连接对象。
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if readonly:
        uri = "file:%s?mode=ro" % path.as_posix()
        conn = sqlite3.connect(uri, uri=True, timeout=10.0, check_same_thread=False)
    else:
        conn = sqlite3.connect(str(path), timeout=15.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    if not readonly:
        ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    current = get_meta(conn, "schema_version")
    if current is None:
        set_meta(conn, "schema_version", str(SCHEMA_VERSION))
    elif int(current) < SCHEMA_VERSION:
        migrate(conn, int(current))
    conn.commit()


def migrate(conn: sqlite3.Connection, from_version: int) -> None:
    """按版本顺序升级；当前只有 v1，保留扩展点。"""
    version = from_version
    while version < SCHEMA_VERSION:
        version += 1
    set_meta(conn, "schema_version", str(version))


def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM schema_meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    conn.commit()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(text: Optional[str], default: Any) -> Any:
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def query_all(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
    return list(conn.execute(sql, tuple(params)).fetchall())


def query_one(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
    return conn.execute(sql, tuple(params)).fetchone()


def execute(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
    cursor = conn.execute(sql, tuple(params))
    conn.commit()
    return cursor


def table_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for table in ("endpoint", "test_case", "suite", "environment", "run", "run_case_result", "import_batch"):
        row = conn.execute("SELECT COUNT(*) AS c FROM %s" % table).fetchone()
        counts[table] = int(row["c"]) if row else 0
    return counts


def vacuum_runs(conn: sqlite3.Connection, keep: int) -> int:
    """只保留最近 keep 次运行，避免数据目录无限增长。"""
    if keep <= 0:
        return 0
    rows = query_all(conn, "SELECT id FROM run ORDER BY started_at DESC, rowid DESC LIMIT -1 OFFSET ?", (keep,))
    stale: Iterable[sqlite3.Row] = rows
    removed = 0
    for row in stale:
        run_id = row["id"]
        execute(conn, "DELETE FROM run_case_result WHERE run_id = ?", (run_id,))
        execute(conn, "DELETE FROM run WHERE id = ?", (run_id,))
        removed += 1
    return removed
