"""导入器包：OpenAPI / Postman / Apifox / curl / Spring 源码。

对外统一入口：

* :class:`ImportOptions` —— 导入选项（字段与 INTERFACES.md 1.7 一致）；
* :func:`detect_format` —— 按内容识别 ``openapi|swagger2|postman|apifox|curl|spring|unknown``；
* :func:`parse_text` / :func:`parse_file` / :func:`parse_document` —— 自动识别并调用对应解析器；
* :func:`parse_openapi` / :func:`parse_postman` / :func:`parse_apifox` / :func:`parse_curl` /
  :func:`scan_spring_project` —— 显式指定格式；
* :data:`SOURCE_LABELS` —— source 标识到中文名的映射，供界面展示。

约定：``ImportOptions`` 在此定义，各子模块通过 ``getattr`` 宽容读取，
避免子模块与包初始化互相 import。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from ..models import ImportResult
from .yaml_lite import YamlLiteError, load_yaml, load_yaml_lite  # noqa: F401  （对外导出）


@dataclass
class ImportOptions:
    """导入选项（字段名与 INTERFACES.md 1.7 冻结契约一致）。"""

    base_url: str = ""
    folder_prefix: str = ""
    generate_cases: bool = True
    case_suffix: str = " - 冒烟"
    include_deprecated: bool = False
    default_headers: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    server_url: str = ""


SOURCE_LABELS: Dict[str, str] = {
    "openapi": "OpenAPI 3",
    "swagger2": "Swagger 2.0（springfox）",
    "postman": "Postman 集合",
    "apifox": "Apifox 导出",
    "curl": "cURL 命令",
    "spring": "Spring 源码扫描",
    "manual": "手工创建",
    "template": "模板生成",
}

# 子模块在包初始化末尾导入：它们只按字符串注解引用 ImportOptions
from .openapi import parse_openapi  # noqa: E402
from .postman import parse_postman  # noqa: E402
from .apifox import parse_apifox  # noqa: E402
from .curl_import import parse_curl  # noqa: E402
from .spring_scan import scan_spring_project  # noqa: E402

__all__ = [
    "ImportOptions",
    "SOURCE_LABELS",
    "YamlLiteError",
    "load_yaml",
    "load_yaml_lite",
    "detect_format",
    "parse_document",
    "parse_text",
    "parse_file",
    "parse_openapi",
    "parse_postman",
    "parse_apifox",
    "parse_curl",
    "scan_spring_project",
]

_SPRING_ANNOTATION_RE = re.compile(r"@(RestController|Controller|RequestMapping|GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping)\b")
_FORMATS = ("openapi", "swagger2", "postman", "apifox", "curl", "spring")


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _looks_like_spring(text: str) -> bool:
    if "@RestController" in text or "@Controller" in text:
        return "class " in text
    if _SPRING_ANNOTATION_RE.search(text) and "class " in text:
        return True
    return False


def _try_parse(text: str) -> Any:
    """先按 JSON 解析，失败再按 YAML 子集解析；都失败返回 None。"""
    stripped = text.strip()
    if not stripped:
        return None
    if stripped[0] in "[{":
        try:
            return json.loads(stripped)
        except ValueError:
            pass
    try:
        return load_yaml_lite(text)
    except YamlLiteError:
        return None


def _detect_document_format(document: Any) -> str:
    """按已解析的文档结构识别格式。"""
    if isinstance(document, Mapping):
        if "openapi" in document:
            return "openapi"
        if "swagger" in document:
            return "swagger2"
        info = _as_mapping(document.get("info"))
        schema = info.get("schema")
        if isinstance(schema, str) and "getpostman.com" in schema:
            return "postman"
        if "item" in document and "info" in document:
            return "postman"
        for key in ("apiCollection", "apiFolder", "apis", "folders", "project", "environments", "schemas"):
            if key in document:
                return "apifox"
    return "unknown"


def detect_format(text: str) -> str:
    """按内容识别导入格式，返回 ``openapi|swagger2|postman|apifox|curl|spring|unknown``。"""
    raw = text if isinstance(text, str) else ""
    if raw.startswith("\ufeff"):
        raw = raw[1:]
    stripped = raw.strip()
    if not stripped:
        return "unknown"
    head = stripped[:120].lstrip("$># \t")
    if re.match(r"^curl(\s|$)", head):
        return "curl"
    if _looks_like_spring(raw):
        return "spring"
    document = _try_parse(raw)
    fmt = _detect_document_format(document)
    if fmt != "unknown":
        return fmt
    lowered = raw.lower()
    if '"openapi"' in lowered or re.search(r"^\s*openapi\s*:", lowered, re.M):
        return "openapi"
    if '"swagger"' in lowered or re.search(r"^\s*swagger\s*:", lowered, re.M):
        return "swagger2"
    if "getpostman.com" in lowered:
        return "postman"
    return "unknown"


def _unknown_format_error(source_ref: str = "") -> ValueError:
    hint = "（来源: %s）" % source_ref if source_ref else ""
    return ValueError(
        "无法识别导入格式%s：支持 OpenAPI 3 / Swagger 2.0 JSON 或 YAML、"
        "Postman 集合、Apifox 导出、curl 命令、Spring 项目目录" % hint
    )


def parse_document(document: Any, fmt: str = "", source_ref: str = "", options: Optional[ImportOptions] = None) -> ImportResult:
    """按已解析的文档分派到具体解析器；``fmt`` 为空时自动识别。"""
    kind = fmt or _detect_document_format(document)
    if kind == "openapi" or kind == "swagger2":
        return parse_openapi(document, source_ref, options)
    if kind == "postman":
        return parse_postman(document, source_ref, options)
    if kind == "apifox":
        return parse_apifox(document, source_ref, options)
    if kind == "curl":
        raise ValueError("curl 命令请使用 parse_curl(command, options)")
    if kind == "spring":
        raise ValueError("Spring 源码请使用 scan_spring_project(项目目录, options)")
    raise _unknown_format_error(source_ref)


def parse_text(text: str, source_ref: str = "", options: Optional[ImportOptions] = None) -> ImportResult:
    """自动识别格式并解析文本内容。"""
    if not isinstance(text, str):
        raise ValueError("导入内容必须是文本")
    fmt = detect_format(text)
    if fmt == "curl":
        return parse_curl(text, options)
    if fmt == "spring":
        raise ValueError("检测到 Spring 源码，请使用 scan_spring_project(项目目录) 或把项目根目录传给 parse_file")
    if fmt == "unknown":
        raise _unknown_format_error(source_ref)
    document = _try_parse(text)
    if document is None:
        raise ValueError("导入内容既不是合法 JSON，也不是合法 YAML%s" % ("（来源: %s）" % source_ref if source_ref else ""))
    return parse_document(document, fmt, source_ref, options)


def parse_file(path: Any, options: Optional[ImportOptions] = None) -> ImportResult:
    """读取文件/目录并自动导入；``.yaml/.yml`` 会先经 yaml_lite 解析。"""
    target = Path(path)
    if target.is_dir():
        return scan_spring_project(target, options)
    if not target.exists():
        raise ValueError("文件不存在: %s" % target)
    ref = str(target)
    suffix = target.suffix.lower()
    if suffix == ".java":
        return scan_spring_project(target, options)
    text = target.read_text(encoding="utf-8-sig")
    if suffix in (".yaml", ".yml"):
        document = load_yaml(text)
        return parse_document(document, "", ref, options)
    if suffix == ".json":
        document = _try_parse(text)
        if document is None:
            raise ValueError("JSON 解析失败: %s" % ref)
        return parse_document(document, "", ref, options)
    return parse_text(text, ref, options)
