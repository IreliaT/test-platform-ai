"""平台统一异常。

服务层按异常类型映射 HTTP 状态码，避免在业务代码里散落状态码判断。
"""

from __future__ import annotations

from typing import Any, List, Optional


class HapiTestError(Exception):
    """所有平台异常的基类。"""

    code = "internal_error"
    status = 500

    def __init__(self, message: str, details: Optional[List[Any]] = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = list(details or [])

    def to_dict(self) -> dict:
        payload = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


class ValidationError(HapiTestError):
    code = "bad_request"
    status = 400


class NotFoundError(HapiTestError):
    code = "not_found"
    status = 404


class ConflictError(HapiTestError):
    code = "conflict"
    status = 409


class MethodNotAllowedError(HapiTestError):
    code = "method_not_allowed"
    status = 405


class BlockedError(HapiTestError):
    """请求被安全策略拦截（如目标主机不允许）。"""

    code = "blocked"
    status = 403


class QueueFullError(HapiTestError):
    code = "queue_full"
    status = 429
