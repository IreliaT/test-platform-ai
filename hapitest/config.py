"""平台配置：目录布局、端口、超时与安全开关。

配置优先级：构造参数 > 环境变量 > `.env.local` > 默认值。
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .errors import ValidationError

DEFAULT_PORT = 8801
ENV_PREFIX = "HAPITEST_"


def project_root() -> Path:
    """平台源码根目录（本文件位于 <root>/hapitest/config.py）。"""
    return Path(__file__).resolve().parent.parent


def load_env_file(path: Path) -> Dict[str, str]:
    """极简 .env 解析：KEY=VALUE，# 注释，支持引号，不做变量展开。"""
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _split_csv(value: Optional[str]) -> Tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in str(value).split(",") if item.strip())


@dataclass
class PlatformConfig:
    """运行时配置。"""

    root_dir: Path = field(default_factory=project_root)
    data_dir: Optional[Path] = None
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    default_timeout_ms: int = 15000
    connect_timeout_ms: int = 5000
    max_response_bytes: int = 262144
    capture_body_bytes: int = 65536
    max_workers: int = 4
    run_retention: int = 300
    allow_hosts: Tuple[str, ...] = ()
    deny_hosts: Tuple[str, ...] = ("169.254.169.254", "metadata.google.internal")
    log_level: str = "INFO"
    extra: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.root_dir = Path(self.root_dir).resolve()
        if self.data_dir is None:
            self.data_dir = self.root_dir / "data"
        self.data_dir = Path(self.data_dir).resolve()
        self.root_dir = self.root_dir

    # --- 目录 ---
    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    @property
    def registry_path(self) -> Path:
        return self.data_dir / "registry.json"

    @property
    def web_dir(self) -> Path:
        return self.root_dir / "hapitest" / "web"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    def project_dir(self, project_id: str) -> Path:
        return self.projects_dir / safe_project_id(project_id)

    def project_db_path(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "platform.db"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.projects_dir, self.reports_dir, self.logs_dir):
            path.mkdir(parents=True, exist_ok=True)

    def allow_hosts_set(self) -> Tuple[str, ...]:
        return tuple(item.lower() for item in self.allow_hosts)

    def deny_hosts_set(self) -> Tuple[str, ...]:
        return tuple(item.lower() for item in self.deny_hosts)


_PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


def safe_project_id(project_id: str) -> str:
    """项目 ID 只允许小写字母、数字、下划线、连字符，防止路径穿越。"""
    text = str(project_id or "").strip().lower()
    if not _PROJECT_ID_RE.match(text):
        raise ValidationError("非法项目 ID: %r（只允许 a-z0-9_-，长度 1-63）" % project_id)
    return text


def slugify(text: str, fallback: str = "project") -> str:
    """把任意名称转成合法项目 ID。

    纯中文等无法生成 ASCII slug 的名称使用「fallback + 名称哈希」，
    保证不同名称不会互相冲突。
    """
    lowered = str(text or "").strip().lower()
    slug = re.sub(r"[^a-z0-9_-]+", "-", lowered).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug or not _PROJECT_ID_RE.match(slug):
        digest = hashlib.sha1(str(text or "").encode("utf-8")).hexdigest()[:6]
        slug = "%s-%s" % (fallback, digest)
    return slug[:63]


def load_config(root_dir: Optional[Path] = None, env_file: Optional[Path] = None, **overrides) -> PlatformConfig:
    """加载配置。overrides 中显式传入的值优先级最高。"""
    root = Path(root_dir).resolve() if root_dir else project_root()
    file_values = load_env_file(env_file or (root / ".env.local"))
    file_values.update(load_env_file(root / ".env"))

    def read(name: str, default: Optional[str] = None) -> Optional[str]:
        key = ENV_PREFIX + name
        if key in os.environ:
            return os.environ[key]
        if key in file_values:
            return file_values[key]
        return default

    data_dir = overrides.pop("data_dir", None)
    if data_dir is None:
        data_dir = read("DATA_DIR")
    config = PlatformConfig(
        root_dir=root,
        data_dir=Path(data_dir).expanduser() if data_dir else None,
        host=str(overrides.pop("host", None) or read("HOST", "127.0.0.1")),
        port=int(overrides.pop("port", None) or read("PORT", str(DEFAULT_PORT))),
        default_timeout_ms=int(overrides.pop("default_timeout_ms", None) or read("TIMEOUT_MS", "15000")),
        connect_timeout_ms=int(overrides.pop("connect_timeout_ms", None) or read("CONNECT_TIMEOUT_MS", "5000")),
        max_response_bytes=int(overrides.pop("max_response_bytes", None) or read("MAX_RESPONSE_BYTES", "262144")),
        capture_body_bytes=int(overrides.pop("capture_body_bytes", None) or read("CAPTURE_BODY_BYTES", "65536")),
        max_workers=int(overrides.pop("max_workers", None) or read("MAX_WORKERS", "4")),
        run_retention=int(overrides.pop("run_retention", None) or read("RUN_RETENTION", "300")),
        allow_hosts=_split_csv(read("ALLOW_HOSTS")),
        deny_hosts=_split_csv(read("DENY_HOSTS", "169.254.169.254,metadata.google.internal")),
        log_level=str(read("LOG_LEVEL", "INFO")),
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    config.ensure_dirs()
    return config


def config_summary(config: PlatformConfig) -> List[str]:
    return [
        "root_dir=%s" % config.root_dir,
        "data_dir=%s" % config.data_dir,
        "listen=%s:%s" % (config.host, config.port),
        "timeout_ms=%s" % config.default_timeout_ms,
        "allow_hosts=%s" % (",".join(config.allow_hosts) or "<all>"),
        "deny_hosts=%s" % (",".join(config.deny_hosts) or "<none>"),
    ]
