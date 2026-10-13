"""HapiTest 命令行入口。

    python3 -m hapitest serve
    python3 -m hapitest project list
    python3 -m hapitest import --project demo --file spec.json
    python3 -m hapitest run --project demo --suite 1 --junit out/junit.xml
    python3 -m hapitest verify

退出码（便于 CI 区分"代码回归"与"环境没配好"）：

====  ==========================================
0     全部通过
1     存在断言失败
2     用法/配置错误（参数不合法、项目不存在）
3     缺少执行条件或请求未完成（error/incomplete）
====  ==========================================
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import PLATFORM_NAME, __version__
from .config import PlatformConfig, config_summary, load_config
from .errors import HapiTestError, NotFoundError, ValidationError
from .models import RunResult
from .repository import ProjectRegistry
from .runner import RunManager, RunRequest
from .sync import (
    apply_import,
    detected_hints,
    fetch_spec,
    load_import,
    quality_warnings,
    seed_environment_base_url,
)

EXIT_OK = 0
EXIT_TEST_FAILED = 1
EXIT_USAGE = 2
EXIT_ENVIRONMENT = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hapitest", description="%s %s 接口自动化测试平台" % (PLATFORM_NAME, __version__))
    parser.add_argument("--data-dir", help="数据目录（默认 <项目根>/data）")
    parser.add_argument("--host", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, help="监听端口（默认 8801）")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="启动 Web 控制台")
    serve.add_argument("--no-browser", action="store_true", help="不打印访问地址提示")
    serve.add_argument("--demo", action="store_true", help="首次启动时创建演示项目与示例接口")
    # 允许把全局参数写在子命令之后（default=SUPPRESS 保证不会覆盖全局值）
    serve.add_argument("--host", default=argparse.SUPPRESS, help="监听地址（默认 127.0.0.1）")
    serve.add_argument("--port", type=int, default=argparse.SUPPRESS, help="监听端口（默认 8801）")
    serve.add_argument("--data-dir", default=argparse.SUPPRESS, help="数据目录")

    project = sub.add_parser("project", help="项目管理")
    project_sub = project.add_subparsers(dest="action", required=True)
    project_sub.add_parser("list", help="列出项目")
    create = project_sub.add_parser("create", help="创建项目")
    create.add_argument("--id", dest="project_id")
    create.add_argument("--name", required=True)
    create.add_argument("--base-url", default="")
    create.add_argument("--description", default="")
    show = project_sub.add_parser("show", help="查看项目")
    show.add_argument("--project", required=True)
    delete = project_sub.add_parser("delete", help="删除项目及其数据")
    delete.add_argument("--project", required=True)
    delete.add_argument("--yes", action="store_true", help="确认删除")

    imp = sub.add_parser("import", help="导入接口（OpenAPI/Swagger/Postman/Apifox/curl/Spring 源码）")
    imp.add_argument("--project", required=True)
    imp.add_argument("--file", help="本地文件路径")
    imp.add_argument("--url", help="在线文档地址，例如 http://127.0.0.1:8081/v2/api-docs?group=api-3")
    imp.add_argument("--text", help="直接传入文档文本")
    imp.add_argument("--curl", help="从 curl 命令导入单个接口")
    imp.add_argument("--scan", help="扫描 Java/Spring 源码目录")
    # MCP（Apifox MCP Server）：用 --mcp-project 触发；令牌只从环境变量/.env.local 读取
    imp.add_argument("--mcp-project", help="通过 MCP 从 Apifox 项目同步（Apifox 项目 ID）")
    imp.add_argument("--mcp-doc", help="通过 MCP 同步 Apifox 公开发布文档（文档 ID）")
    imp.add_argument("--mcp-oas", help="通过 MCP 读取本地 OpenAPI 文件（路径）")
    imp.add_argument("--mcp-command", default="", help="自定义 MCP 服务启动命令，默认 npx -y apifox-mcp-server@latest")
    imp.add_argument("--mcp-token-env", default="APIFOX_ACCESS_TOKEN", help="存放访问令牌的环境变量名")
    imp.add_argument("--mcp-timeout", type=float, default=180.0, help="MCP 调用超时（秒）")
    imp.add_argument("--mcp-api-base-url", default="", help="Apifox 私有化部署地址")
    imp.add_argument("--mcp-branch-id", default="", help="Apifox 分支 ID（不填读主分支）")
    imp.add_argument("--no-mcp-refresh", action="store_true", help="不强制刷新，直接用服务端缓存")
    imp.add_argument("--source", default="auto", help="auto|openapi|swagger2|postman|apifox|curl|spring")
    imp.add_argument("--mode", default="merge", choices=("merge", "replace"))
    imp.add_argument("--base-url", default="", help="覆盖 base_url")
    imp.add_argument("--folder-prefix", default="")
    imp.add_argument("--no-cases", action="store_true", help="不自动生成冒烟用例")
    imp.add_argument("--include-deprecated", action="store_true")
    imp.add_argument("--preview", action="store_true", help="只解析不入库")
    imp.add_argument("--refresh-cases", action="store_true",
                     help="用契约里的模板与断言覆盖已存在的生成用例（默认只增不改）")
    imp.add_argument("--header", action="append", default=[], help="抓取文档时附加的请求头，形如 'X-Token: abc'")

    run = sub.add_parser("run", help="执行用例或套件")
    run.add_argument("--project", required=True)
    run.add_argument("--suite", type=int)
    run.add_argument("--case", dest="cases", type=int, action="append", default=[])
    run.add_argument("--all", action="store_true", help="执行项目内全部启用用例")
    run.add_argument("--tag", help="只执行带该标签的用例（与 --all 搭配）")
    run.add_argument("--env", type=int)
    run.add_argument("--set", dest="variables", action="append", default=[], help="运行变量 key=value")
    run.add_argument("--junit", help="JUnit XML 输出路径")
    run.add_argument("--report-dir", help="生成本地报告目录（xml/html/json）")
    run.add_argument("--json", dest="json_out", help="运行摘要 JSON 输出路径")
    run.add_argument("--timeout", type=float, default=900.0, help="整体超时（秒）")
    run.add_argument("--quiet", action="store_true")

    sub.add_parser("doctor", help="体检：配置、目录、项目、最近运行")
    verify = sub.add_parser("verify", help="端到端自检（本地 mock 服务，不访问外网）")
    verify.add_argument("--json", dest="json_out", help="把自检报告写入指定路径")
    verify.add_argument("--keep", action="store_true", help="保留自检产生的项目数据")
    return parser


def make_config(args: argparse.Namespace) -> PlatformConfig:
    overrides: Dict[str, Any] = {}
    if getattr(args, "data_dir", None):
        overrides["data_dir"] = Path(args.data_dir).expanduser()
    if getattr(args, "host", None):
        overrides["host"] = args.host
    if getattr(args, "port", None):
        overrides["port"] = args.port
    return load_config(**overrides)


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


# --------------------------------------------------------------------- 子命令
def cmd_serve(args: argparse.Namespace, config: PlatformConfig) -> int:
    from .server.app import serve

    if args.demo:
        bootstrap_demo(config)
    print("%s %s 启动中：http://%s:%d/" % (PLATFORM_NAME, __version__, config.host, config.port))
    print("数据目录：%s" % config.data_dir)
    serve(config)
    return EXIT_OK


def cmd_project(args: argparse.Namespace, config: PlatformConfig) -> int:
    registry = ProjectRegistry(config)
    action = args.action
    if action == "list":
        records = registry.list_projects()
        if not records:
            print("暂无项目。使用 `python3 -m hapitest project create --name 我的项目` 创建。")
            return EXIT_OK
        print("%-24s %-28s %-40s %s" % ("ID", "名称", "BASE_URL", "接口/用例/套件/运行"))
        for record in records:
            counts = registry.repository(record.id).counts()
            print(
                "%-24s %-28s %-40s %d/%d/%d/%d"
                % (
                    record.id,
                    record.name,
                    record.base_url or "-",
                    counts.get("endpoint", 0),
                    counts.get("test_case", 0),
                    counts.get("suite", 0),
                    counts.get("run", 0),
                )
            )
        return EXIT_OK
    if action == "create":
        record = registry.create(
            name=args.name,
            project_id=args.project_id,
            description=args.description,
            base_url=args.base_url,
        )
        print("已创建项目 %s（%s）" % (record.id, record.name))
        return EXIT_OK
    if action == "show":
        record = registry.get(args.project)
        counts = registry.repository(record.id).counts()
        print(json.dumps({"project": record.to_dict(), "counts": counts}, ensure_ascii=False, indent=2))
        return EXIT_OK
    if action == "delete":
        if not args.yes:
            print("拒绝执行：删除项目需要 --yes 确认", file=sys.stderr)
            return EXIT_USAGE
        registry.delete(args.project)
        print("已删除项目 %s" % args.project)
        return EXIT_OK
    raise ValidationError("未知操作: %s" % action)


def cmd_import(args: argparse.Namespace, config: PlatformConfig) -> int:
    registry = ProjectRegistry(config)
    record = registry.get(args.project)
    repository = registry.repository(record.id)

    options = {
        "base_url": args.base_url,
        "folder_prefix": args.folder_prefix,
        "generate_cases": not args.no_cases,
        "include_deprecated": args.include_deprecated,
    }
    if args.scan:
        from .importers import ImportOptions, scan_spring_project
        from .sync import build_options

        root = Path(args.scan).expanduser().resolve()
        if not root.exists():
            raise ValidationError("源码目录不存在: %s" % root)
        result = scan_spring_project(root, options=build_options(options))
        result.source = "spring"
    elif args.mcp_project or args.mcp_doc or args.mcp_oas:
        from .sync import load_import_from_mcp

        command = shlex.split(args.mcp_command) if args.mcp_command else None
        result, meta = load_import_from_mcp(
            config,
            project_id=args.mcp_project,
            doc_id=args.mcp_doc,
            spec_file=args.mcp_oas,
            command=command,
            token_env=args.mcp_token_env,
            refresh=not args.no_mcp_refresh,
            timeout_s=args.mcp_timeout,
            api_base_url=(args.mcp_api_base_url or None),
            branch_id=(args.mcp_branch_id or None),
            options=options,
        )
        print(
            "MCP 同步：服务=%s，分支=%s，工具=%s，刷新=%s，取回 %s 字节"
            % (
                meta.get("server", "?"),
                meta.get("branch_id", "?"),
                meta.get("tool", "?"),
                "是" if meta.get("refreshed") else "否（使用缓存）",
                meta.get("bytes"),
            )
        )
    elif args.curl:
        result = load_import(config, source="curl", text=args.curl, options=options)
    elif args.url:
        headers = {}
        for item in args.header:
            if ":" in item:
                key, _, value = item.partition(":")
                headers[key.strip()] = value.strip()
        text, final_url, size = fetch_spec(config, args.url, headers=headers)
        result = load_import(config, source=args.source, text=text, options=options)
        print("已抓取 %s（%d 字节）" % (final_url, size))
    else:
        result = load_import(config, source=args.source, text=args.text, path=args.file, options=options)

    hints = detected_hints(result)
    warnings = list(result.stats.warnings) + quality_warnings(result)
    print(
        "解析完成：来源=%s，接口 %d 个，用例 %d 条，警告 %d 条"
        % (result.source or args.source, len(result.endpoints), len(result.cases), len(warnings))
    )
    for warning in warnings[:10]:
        print("  ! %s" % warning)
    if len(warnings) > 10:
        print("  …（其余 %d 条警告已省略）" % (len(warnings) - 10))
    if args.preview:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2)[:4000])
        return EXIT_OK

    stats = apply_import(repository, result, mode=args.mode, refresh_cases=args.refresh_cases)
    discovered = hints.get("detected_server_url", "")
    if discovered and not repository.resolve_environment(None).base_url:
        seed_environment_base_url(repository, result, discovered)
        print("已把探测到的地址写入默认环境：%s" % discovered)
    repository.record_import(stats, title=result.title or record.name)
    print(
        "入库完成：新增 %d，更新 %d，未变化 %d，新增用例 %d，刷新用例 %d"
        % (stats.created, stats.updated, stats.unchanged, stats.cases_created, stats.cases_updated)
    )
    return EXIT_OK


def cmd_run(args: argparse.Namespace, config: PlatformConfig) -> int:
    registry = ProjectRegistry(config)
    registry.get(args.project)
    case_ids = list(args.cases)
    if args.all or args.tag:
        repository = registry.repository(args.project)
        cases, _ = repository.list_cases(enabled=True, tag=(args.tag or ""), limit=5000)
        case_ids = [int(case.id) for case in cases if case.id is not None]
        if not case_ids:
            print("没有匹配的启用用例", file=sys.stderr)
            return EXIT_USAGE
    manager = RunManager(config, registry)
    variables: Dict[str, str] = {}
    for item in args.variables:
        if "=" not in item:
            raise ValidationError("--set 需要 key=value 形式: %s" % item)
        key, _, value = item.partition("=")
        variables[key.strip()] = value
    request = RunRequest(
        project_id=args.project,
        case_ids=case_ids,
        suite_id=args.suite,
        env_id=args.env,
        trigger="cli",
        note="命令行执行",
        variables=variables,
    )
    manager.start()
    try:
        run = manager.run_sync(request, timeout_s=args.timeout)
    finally:
        manager.stop()
    if not args.quiet:
        print_run_summary(run)
    write_outputs(args, registry, run)
    return run.exit_code


def write_outputs(args: argparse.Namespace, registry: ProjectRegistry, run: RunResult) -> None:
    from .report import to_json_report, to_junit_xml, write_reports

    if args.report_dir:
        paths = write_reports(run, Path(args.report_dir).expanduser(), project_name=args.project)
        if not args.quiet:
            for kind, path in sorted(paths.items()):
                print("报告（%s）：%s" % (kind, path))
    if args.junit:
        target = Path(args.junit).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(to_junit_xml(run), encoding="utf-8")
        if not args.quiet:
            print("JUnit XML：%s" % target)
    if args.json_out:
        target = Path(args.json_out).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(to_json_report(run), encoding="utf-8")
        if not args.quiet:
            print("运行 JSON：%s" % target)


def print_run_summary(run: RunResult) -> None:
    print(
        "\n运行 %s 状态=%s 通过 %d/%d（失败 %d，错误 %d，未完成 %d，跳过 %d）耗时 %.2fs"
        % (
            run.id,
            run.status,
            run.passed,
            run.total,
            run.failed,
            run.errored,
            run.incomplete,
            run.skipped,
            run.duration_ms / 1000.0,
        )
    )
    for result in run.case_results:
        marker = {"passed": "PASS", "failed": "FAIL", "error": "ERR ", "incomplete": "INC ", "skipped": "SKIP"}.get(
            result.status, result.status.upper()
        )
        print("  [%s] %s (%dms)" % (marker, result.case_name, result.duration_ms))
        for assertion in result.assertions:
            flag = "ok  " if assertion.passed else "fail"
            print("        %s %s → 实际 %s" % (flag, assertion.describe, _short(assertion.actual)))
        if result.error:
            print("        error: %s" % result.error)


def _short(value: Any, limit: int = 120) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = str(text).replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def cmd_doctor(args: argparse.Namespace, config: PlatformConfig) -> int:
    from .doctor import run_doctor

    report = run_doctor(config)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return EXIT_OK if report["status"] == "ready" else EXIT_ENVIRONMENT


def cmd_verify(args: argparse.Namespace, config: PlatformConfig) -> int:
    from .selfcheck import run_selfcheck

    report = run_selfcheck(config, keep=args.keep)
    print(json.dumps({k: v for k, v in report.items() if k != "details"}, ensure_ascii=False, indent=2))
    if args.json_out:
        target = Path(args.json_out).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("自检报告：%s" % target)
    return EXIT_OK if report["status"] == "passed" else EXIT_TEST_FAILED


def bootstrap_demo(config: PlatformConfig) -> None:
    """创建演示项目并把内置示例规格导入，便于第一次启动就能看到效果。

    幂等：项目已存在时只补齐缺失的演示套件，不重复导入。
    """
    from .models import SuiteSpec

    registry = ProjectRegistry(config)
    if not registry.exists("demo"):
        record = registry.create(name="演示项目", project_id="demo", description="内置示例：本地 mock 服务 + Spring 风格接口")
        repository = registry.repository(record.id)
        spec_path = config.root_dir / "examples" / "demo_specs" / "demo_openapi.json"
        if spec_path.exists():
            result = load_import(config, path=str(spec_path), options={"base_url": "", "generate_cases": True})
            apply_import(repository, result, mode="merge")
        environment = repository.default_environment()
        if environment is not None and not environment.base_url:
            environment.base_url = "http://127.0.0.1:8899"
            repository.update_environment(environment.id, environment)
    repository = registry.repository("demo")
    suites, _ = repository.list_suites(limit=100)
    if not any(suite.name == "演示套件" for suite in suites):
        cases, _ = repository.list_cases(enabled=True, limit=1000)
        case_ids = [int(case.id) for case in cases if case.id is not None]
        environment = repository.default_environment()
        if case_ids:
            repository.create_suite(
                SuiteSpec(
                    name="演示套件",
                    description="导入生成的冒烟用例，可直接运行",
                    case_ids=case_ids,
                    env_id=(environment.id if environment else None),
                )
            )
    print("演示项目 demo 已就绪：先运行 `python3 examples/mock_service.py`，再在界面上运行「演示套件」。")


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(getattr(args, "verbose", False))
    try:
        config = make_config(args)
    except Exception as exc:
        print("配置错误：%s" % exc, file=sys.stderr)
        return EXIT_USAGE
    handlers = {
        "serve": cmd_serve,
        "project": cmd_project,
        "import": cmd_import,
        "run": cmd_run,
        "doctor": cmd_doctor,
        "verify": cmd_verify,
    }
    handler = handlers.get(args.command)
    if handler is None:
        parser.print_help()
        return EXIT_USAGE
    try:
        return handler(args, config)
    except ValidationError as exc:
        print("参数错误：%s" % exc.message, file=sys.stderr)
        for detail in exc.details:
            print("  - %s" % detail, file=sys.stderr)
        return EXIT_USAGE
    except NotFoundError as exc:
        print("未找到：%s" % exc.message, file=sys.stderr)
        return EXIT_USAGE
    except HapiTestError as exc:
        print("执行失败：%s" % exc.message, file=sys.stderr)
        return EXIT_ENVIRONMENT
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
