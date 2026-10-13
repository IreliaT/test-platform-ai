"""变量解析与响应提取。

变量语法（:data:`VAR_PATTERN`）
------------------------------
======================  ==================================================
语法                    说明
======================  ==================================================
``${name}``            引用变量
``{{name}}``           引用变量（双花括号写法）
``${name:-fallback}``  变量缺失或为空时使用 ``fallback``
``${name?}``           变量缺失时保留原文，且**不**记入缺失列表
======================  ==================================================

变量名允许 ``[A-Za-z_][A-Za-z0-9_.-]*``；``{{name}}`` 同样支持 ``:-`` 与 ``?``。

解析规则
--------
- **单次解析**：只做一层变量替换，替换结果里如果又出现 ``${...}`` 不会再展开，
  也不支持变量引用变量（避免循环引用）；``resolve_value`` 的「递归」仅指递归遍历
  嵌套的 dict/list，不指变量展开。
- 变量层按「先加入的优先级更高」查找：``layers[0]`` 最先命中。
  ``set()`` 写入最高优先级的覆盖层（首次写入时创建），不会修改调用方传入的字典。
- ``resolve_text`` 永远返回 ``str``；``resolve_value`` 在「整个字符串恰好是一个占位符」时
  返回变量的**原始类型**（dict/list/int/None 等），混合文本仍返回字符串。
- 未命中的变量保留原文并记录到 ``unresolved_names()``；
  ``missing_names()`` 是它的别名（同一个列表，历史接口保留）。
  带 ``:-`` 兜底或带 ``?`` 的占位符不会记入。
"""

from __future__ import annotations

import json
import re
from http.cookies import SimpleCookie
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import jsonpath, redact
from .models import ExtractorSpec, HttpResponse

__all__ = [
    "VAR_PATTERN",
    "VariableResolver",
    "build_resolver",
    "extract_value",
    "apply_extractors",
]

# 匹配 ${name} / {{name}} / ${name:-default} / ${name?}
# 花括号数量必须配对：${a} 用 }，{{a}} 用 }}
VAR_PATTERN = re.compile(
    r"(?:(?P<dollar>\$\{)|(?P<brace>\{\{))"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_.-]*)"
    r"(?:(?P<default_mark>:-)(?P<default>[^{}]*))?"
    r"(?P<optional>\?)?"
    r"(?(dollar)\}|(?(brace)\}\}))"
)

_COOKIE_ATTRIBUTES = (
    "path",
    "domain",
    "expires",
    "max-age",
    "secure",
    "httponly",
    "samesite",
    "version",
    "comment",
)


def _is_empty(value: Any) -> bool:
    """``None``、空字符串、空容器视为「空」，``0`` / ``False`` 不算空。"""
    if value is None:
        return True
    if isinstance(value, str):
        return value == ""
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


def _stringify(value: Any) -> str:
    """把取值渲染成文本：``None`` → 空串，布尔 → ``true``/``false``，容器 → JSON。"""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


class VariableResolver:
    """多层变量解析器：先加入的层优先级更高。"""

    def __init__(self, layers: Optional[Sequence[Dict[str, Any]]] = None) -> None:
        self._layers: List[Dict[str, Any]] = []
        self._layer_names: List[str] = []
        self._overrides: Dict[str, Any] = {}
        self._missing: List[str] = []
        if layers:
            if isinstance(layers, Mapping):
                layers = [layers]  # 容错：允许直接传单个字典
            for layer in layers:
                self.add_layer(layer)

    # -- 层管理 -----------------------------------------------------------
    def add_layer(self, values: Mapping[str, Any], name: str = "") -> None:
        """追加一个低优先级变量层（会复制传入的字典，不修改调用方数据）。"""
        if values is None:
            values = {}
        if not isinstance(values, Mapping):
            raise TypeError("变量层必须是 Mapping，实际为 %s" % type(values).__name__)
        self._layers.append(dict(values))
        self._layer_names.append(str(name or ""))

    def set(self, key: str, value: Any) -> None:
        """写入最高优先级的覆盖层（覆盖所有层，不影响调用方字典）。"""
        self._overrides[str(key)] = value

    def _lookup(self, key: str) -> Tuple[bool, Any]:
        if key in self._overrides:
            return True, self._overrides[key]
        for layer in self._layers:
            if key in layer:
                return True, layer[key]
        return False, None

    def get(self, key: str, default: Any = None) -> Any:
        found, value = self._lookup(key)
        return value if found else default

    # -- 解析 -------------------------------------------------------------
    def _substitute(self, match: "re.Match", raw: bool = False) -> Any:
        name = match.group("name")
        fallback = match.group("default")
        optional = match.group("optional") is not None
        found, value = self._lookup(name)
        if found and not _is_empty(value):
            return value if raw else _stringify(value)
        if fallback is not None:
            return fallback
        if found:
            return value if raw else _stringify(value)
        if optional:
            return match.group(0)
        self._record_missing(name)
        return match.group(0)

    def _record_missing(self, name: str) -> None:
        if name not in self._missing:
            self._missing.append(name)

    def resolve_text(self, text: str) -> str:
        """替换文本中的全部占位符，**始终返回 str**（非字符串取值会被渲染）。"""
        if text is None:
            return ""
        if not isinstance(text, str):
            return _stringify(text)
        return VAR_PATTERN.sub(lambda match: _stringify(self._substitute(match, raw=False)), text)

    def resolve_value(self, value: Any) -> Any:
        """递归解析取值：str 做替换，dict/list/tuple 逐项递归，其它类型原样返回。

        整个字符串恰好是一个占位符时返回变量的原始类型（可能是 dict/list/int/None）。
        """
        if isinstance(value, str):
            match = VAR_PATTERN.fullmatch(value)
            if match is not None:
                return self._substitute(match, raw=True)
            return self.resolve_text(value)
        if isinstance(value, Mapping):
            return {key: self.resolve_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.resolve_value(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.resolve_value(item) for item in value)
        return value

    def resolve_mapping(self, values: Mapping[str, Any]) -> Dict[str, Any]:
        """对字典逐值解析（键保持不变）。"""
        if not isinstance(values, Mapping):
            return {}
        return {key: self.resolve_value(item) for key, item in values.items()}

    # -- 状态 -------------------------------------------------------------
    def unresolved_names(self) -> List[str]:
        """解析过程中未命中（且没有 ``:-`` 兜底、没标 ``?``）的变量名，按首次出现顺序去重。"""
        return list(self._missing)

    def missing_names(self) -> List[str]:
        """``unresolved_names()`` 的别名，返回同一个列表（历史接口保留）。"""
        return list(self._missing)

    def as_dict(self) -> Dict[str, Any]:
        """所有层的合并视图（高优先级覆盖低优先级）。

        注意：可能包含密钥明文，写日志/落库前必须自行脱敏。
        """
        merged: Dict[str, Any] = {}
        for layer in reversed(self._layers):
            merged.update(layer)
        merged.update(self._overrides)
        return merged


def build_resolver(*layers: Mapping[str, Any]) -> VariableResolver:
    """按「先传入的层优先级更高」构造解析器。"""
    return VariableResolver(list(layers))


# ---------------------------------------------------------------------------
# 响应提取
# ---------------------------------------------------------------------------


def _lookup_ci(values: Any, name: str) -> Tuple[bool, Any]:
    """大小写不敏感查找。"""
    if not isinstance(values, Mapping) or not name:
        return False, None
    if name in values:
        return True, values[name]
    lowered = str(name).lower()
    for key, value in values.items():
        if str(key).lower() == lowered:
            return True, value
    return False, None


def _parse_cookie_pairs(value: str) -> Dict[str, str]:
    """解析 ``Set-Cookie`` / ``Cookie`` 头（跳过 Path/Expires 等属性）。

    与 :mod:`hapitest.assertions` 保持一致的解析策略：优先用 ``http.cookies.SimpleCookie``，
    失败时退化为手工切分。
    """
    cookies: Dict[str, str] = {}
    if not value:
        return cookies
    try:
        jar = SimpleCookie()
        jar.load(str(value))
        for key, morsel in jar.items():
            cookies[str(key)] = str(morsel.value)
        if cookies:
            return cookies
    except Exception:
        cookies = {}
    for chunk in str(value).split(";"):
        part = chunk.strip()
        if not part or "=" not in part:
            continue
        name, _sep, raw = part.partition("=")
        name = name.strip()
        if not name or name.lower() in _COOKIE_ATTRIBUTES:
            continue
        cookies[name] = raw.strip().strip('"')
    return cookies


def _cookies_from_response(response: Any) -> Dict[str, str]:
    """响应 ``Set-Cookie`` 优先，其次回退到请求预览里的 ``Cookie`` 头。"""
    cookies: Dict[str, str] = {}
    headers = getattr(response, "headers", None) or {}
    found, value = _lookup_ci(headers, "Set-Cookie")
    if found:
        cookies.update(_parse_cookie_pairs(value))
    preview = getattr(response, "request_preview", None)
    if isinstance(preview, Mapping):
        found, value = _lookup_ci(preview.get("headers"), "Cookie")
        if found:
            for key, item in _parse_cookie_pairs(value).items():
                cookies.setdefault(key, item)
    return cookies


def extract_value(response: HttpResponse, extractor: ExtractorSpec) -> Tuple[bool, Any, str]:
    """按提取器从响应中取值，返回 ``(是否成功, 取值, 中文说明)``。

    失败时取值为 ``extractor.default``，说明为失败原因（中文）。本函数不抛异常。
    """
    default = getattr(extractor, "default", None)
    try:
        source = str(getattr(extractor, "source", "") or "").strip()
        expression = str(getattr(extractor, "expression", "") or "")

        if source in ("json_path", "json_path_all"):
            body = getattr(response, "body_json", None)
            text = getattr(response, "body_text", "") or ""
            if body is None:
                if not str(text).strip():
                    return False, default, "响应体为空，无法按 json_path 提取"
                try:
                    body = json.loads(text)
                except ValueError:
                    return False, default, "响应体不是合法 JSON，无法按 json_path 提取"
            path = expression.strip() or "$"
            try:
                results = jsonpath.query(body, path)
            except jsonpath.JsonPathError as exc:
                return False, default, "JSONPath 表达式无效: %s" % exc
            if source == "json_path_all":
                return True, results, "json_path_all %s 提取成功" % path
            if not results:
                return False, default, "JSONPath 无匹配: %s" % path
            return True, results[0], "json_path %s 提取成功" % path

        if source == "header":
            name = expression.strip()
            if not name:
                return False, default, "header 提取器缺少响应头名称"
            found, value = _lookup_ci(getattr(response, "headers", None) or {}, name)
            if not found:
                return False, default, "响应头不存在: %s" % name
            return True, value, "响应头 %s 提取成功" % name

        if source == "cookie":
            name = expression.strip()
            if not name:
                return False, default, "cookie 提取器缺少 Cookie 名称"
            found, value = _lookup_ci(_cookies_from_response(response), name)
            if not found:
                return False, default, "响应 Cookie 不存在: %s" % name
            return True, value, "Cookie %s 提取成功" % name

        if source == "regex":
            pattern = expression.strip()
            if not pattern:
                return False, default, "regex 提取器缺少正则表达式"
            text = getattr(response, "body_text", "") or ""
            try:
                match = re.search(pattern, str(text))
            except re.error as exc:
                return False, default, "正则表达式无效: %s（%s）" % (pattern, exc)
            if match is None:
                return False, default, "正则未匹配: %s" % pattern
            value = match.group(1) if match.groups() else match.group(0)
            return True, value, "正则 %s 提取成功" % pattern

        if source == "body_text":
            return True, (getattr(response, "body_text", "") or ""), "已提取响应体文本"

        if source == "status_code":
            status = getattr(response, "status", None)
            if status is None:
                return False, default, "响应中没有状态码（请求可能未完成）"
            return True, status, "状态码提取成功"

        if source == "duration_ms":
            return True, int(getattr(response, "duration_ms", 0) or 0), "响应耗时提取成功"

        return False, default, "未知提取来源: %s" % (source or "<空>")
    except Exception as exc:  # 兜底：提取绝不抛异常
        return False, default, "提取异常: %s: %s" % (type(exc).__name__, exc)


def apply_extractors(
    response: HttpResponse,
    extractors: Sequence[ExtractorSpec],
    resolver: VariableResolver,
) -> Tuple[Dict[str, Any], List[str]]:
    """批量执行提取器，返回 ``(用于日志/历史的脱敏字典, 警告列表)``。

    安全约定（重要）
    ----------------
    - 提取成功的变量以**明文**写入 ``resolver``（后续请求需要真实值）；
    - **返回的字典**仅供日志、运行历史与界面展示，敏感变量名（``redact.is_secret_key``）
      的取值会被 ``redact.mask_if_secret`` 替换为掩码，绝不返回明文。

    其它行为：

    - 提取失败时若设置了 ``extractor.default``，把默认值写入 resolver 并在警告中说明「使用默认值」；
      否则写警告「未提取到变量 x」，返回字典中该变量置 ``None``，且不写入 resolver
      （这样 ``${x}`` 仍会被识别为未解析变量）。
    - ``required=True`` 的失败仍会记录警告（警告文案带「必填」前缀），不会静默丢弃。
    - 任何单条提取器的异常都会被捕获并转成警告，本函数不抛异常。
    """
    extracted: Dict[str, Any] = {}
    warnings: List[str] = []
    for extractor in extractors or []:
        try:
            name = str(getattr(extractor, "name", "") or "").strip()
            if not name:
                warnings.append("提取器缺少变量名，已跳过")
                continue
            required = bool(getattr(extractor, "required", False))
            default = getattr(extractor, "default", None)
            ok, value, message = extract_value(response, extractor)
            if ok:
                resolver.set(name, value)
                extracted[name] = None if value is None else redact.mask_if_secret(name, value)
                continue
            if default is not None:
                resolver.set(name, default)
                extracted[name] = None if default is None else redact.mask_if_secret(name, default)
                warnings.append("%s变量 %s 未提取到：%s，使用默认值" % ("必填" if required else "", name, message))
            else:
                extracted[name] = None
                warnings.append("%s变量 %s 未提取到：%s" % ("必填" if required else "", name, message))
        except Exception as exc:  # 兜底：单条提取器异常不影响其它提取器
            warnings.append("提取器执行异常: %s: %s" % (type(exc).__name__, exc))
    return extracted, warnings
