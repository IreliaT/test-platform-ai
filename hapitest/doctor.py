"""体检（doctor）：一条命令看清平台是否可用。

不访问外网、不修改项目数据，只读检查配置、目录、项目库与最近运行。
"""

from __future__ import annotations

import platform
import socket
import sys
from pathlib import Path
from typing import Any, Dict, List

from . import PLATFORM_NAME, PROTOCOL_VERSION, __version__
from .config import PlatformConfig, config_summary
from .repository import ProjectRegistry


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex((host, port)) != 0


def run_doctor(config: PlatformConfig, probe_port: bool = True) -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    add("python", "ready" if sys.version_info >= (3, 9) else "failed", "%s (%d.%d.%d)" % (platform.python_version(), *sys.version_info[:3]))
    add("platform", "ready", "%s %s / 协议 %s" % (PLATFORM_NAME, __version__, PROTOCOL_VERSION))

    try:
        config.ensure_dirs()
        add("data_dir", "ready", str(config.data_dir))
    except OSError as exc:
        add("data_dir", "failed", "无法创建数据目录：%s" % exc)

    writable = True
    probe = config.data_dir / ".doctor-probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        writable = False
        add("data_dir_writable", "failed", "数据目录不可写：%s" % exc)
    if writable:
        add("data_dir_writable", "ready", "可写")

    web_index = config.web_dir / "index.html"
    add(
        "web_assets",
        "ready" if web_index.exists() else "incomplete",
        str(config.web_dir) if web_index.exists() else "缺少前端入口 %s" % web_index,
    )

    registry = ProjectRegistry(config)
    projects = registry.list_projects()
    add("projects", "ready" if projects else "incomplete", "共 %d 个项目" % len(projects))
    for record in projects:
        try:
            counts = registry.repository(record.id).counts()
            add(
                "project:%s" % record.id,
                "ready",
                "接口 %d，用例 %d，套件 %d，运行 %d"
                % (counts.get("endpoint", 0), counts.get("test_case", 0), counts.get("suite", 0), counts.get("run", 0)),
            )
            environment = registry.repository(record.id).resolve_environment(None)
            add(
                "env:%s" % record.id,
                "ready" if (environment.base_url or record.base_url) else "incomplete",
                "环境 %s，base_url=%s" % (environment.name, environment.base_url or record.base_url or "(未配置)"),
            )
        except Exception as exc:
            add("project:%s" % record.id, "failed", "读取失败：%s" % exc)

    if probe_port:
        free = _port_free(config.host, config.port)
        add(
            "port",
            "ready" if free else "incomplete",
            "%s:%d %s" % (config.host, config.port, "可用" if free else "已被占用（启动前请换端口或停止占用进程）"),
        )

    failed = [item for item in checks if item["status"] == "failed"]
    incomplete = [item for item in checks if item["status"] == "incomplete"]
    status = "failed" if failed else ("incomplete" if incomplete else "ready")
    return {
        "status": status,
        "checks": checks,
        "config": config_summary(config),
        "next_actions": _next_actions(status, incomplete, projects),
    }


def _next_actions(status: str, incomplete: List[Dict[str, Any]], projects: List[Any]) -> List[str]:
    actions: List[str] = []
    for item in incomplete:
        if item["name"] == "projects":
            actions.append("创建项目：python3 -m hapitest project create --name 我的项目")
        elif item["name"].startswith("env:"):
            actions.append("在 Web 控制台「环境」页或 CLI 中为项目配置 base_url")
        elif item["name"] == "web_assets":
            actions.append("前端资源缺失，请确认仓库完整（hapitest/web/index.html）")
        elif item["name"] == "port":
            actions.append("端口被占用：改用 --port 指定其它端口")
    if not actions and status == "ready":
        actions.append("启动控制台：python3 -m hapitest serve")
    return actions
