"""api.v0.1 统一错误信封与请求校验工具（S1.3）。

契约 §1.2：错误响应含 error 与 meta；error.code 为稳定机器码，details 元素
{field, reason} 为稳定原因码；框架校验错误统一包装为 422 VALIDATION_ERROR。
不返回堆栈、SQL、供应商地址或凭据。

REVIEW-S1-20260930 F4：SQLite 异常在统一边界分类——可识别的不可用
（表缺失/文件损坏/无法打开）→ 503 DATA_UNAVAILABLE，其余 → 500；
任何未预期异常都返回脱敏的 500 INTERNAL_ERROR 信封。
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from backend.config import Settings
from backend.services import derive

#: 可识别为"数据不可用"的 SQLite 错误特征（REVIEW-S1-20260930 F4）：
#: 空库/未建表、文件损坏、无法打开。其余 sqlite3.Error（含 SQL 编程错误）
#: 不伪装成数据不可用，按内部错误返回。
DATA_UNAVAILABLE_MARKERS = (
    "no such table",
    "file is not a database",
    "database disk image is malformed",
    "unable to open database file",
)


def request_id() -> str:
    return f"req-{uuid.uuid4().hex[:12]}"


def make_meta(settings: Settings, req_id: str, data_version: str | None) -> dict:
    return {
        "api_version": settings.api_version,
        "schema_version": settings.schema_version,
        "data_version": data_version,
        "request_id": req_id,
        "generated_at": datetime.now(derive.CN_TZ).replace(microsecond=0).isoformat(),
        "simulated": settings.simulated,
    }


class ApiError(Exception):
    """业务错误基类：由异常处理器转成统一信封。"""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: list[dict] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or []
        self.retryable = retryable


class ValidationFailed(ApiError):
    def __init__(self, field: str, reason: str, message: str) -> None:
        super().__init__(
            422,
            "VALIDATION_ERROR",
            message,
            details=[{"field": field, "reason": reason}],
        )


class ProductNotFound(ApiError):
    def __init__(self, message: str = "商品不存在") -> None:
        super().__init__(
            404,
            "PRODUCT_NOT_FOUND",
            message,
            details=[{"field": "id", "reason": "not_found"}],
        )


class MerchantNotFound(ApiError):
    def __init__(self) -> None:
        super().__init__(
            404,
            "MERCHANT_NOT_FOUND",
            "商家不存在",
            details=[{"field": "id", "reason": "not_found"}],
        )


class DataUnavailable(ApiError):
    def __init__(self, message: str, field: str = "database") -> None:
        super().__init__(
            503,
            "DATA_UNAVAILABLE",
            message,
            details=[{"field": field, "reason": "data_unavailable"}],
        )


class InternalError(ApiError):
    """未预期异常的脱敏 500 信封：不含 SQL、路径或堆栈。"""

    def __init__(self) -> None:
        super().__init__(500, "INTERNAL_ERROR", "服务内部错误")


def map_sqlite_error(exc: sqlite3.Error) -> ApiError:
    """F4：可识别的 SQLite 不可用错误 → 503 DATA_UNAVAILABLE；其余 → 500。"""
    text = str(exc).lower()
    if any(marker in text for marker in DATA_UNAVAILABLE_MARKERS):
        return DataUnavailable("数据库不可用或已损坏，请重新运行 scripts/init_database.py")
    return InternalError()


def error_response(
    settings: Settings,
    req_id: str,
    data_version: str | None,
    error: ApiError,
) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content={
            "error": {
                "code": error.code,
                "message": error.message,
                "details": error.details,
                "retryable": error.retryable,
            },
            "meta": make_meta(settings, req_id, data_version),
        },
    )


def install_error_handlers(application, get_data_version) -> None:
    """注册统一错误处理器；get_data_version(app) 尽力取批次号，失败为 null。"""

    @application.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        settings: Settings = request.app.state.settings
        req_id = request_id()
        return error_response(settings, req_id, get_data_version(request.app), exc)

    @application.exception_handler(RequestValidationError)
    async def _framework_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """框架校验错误统一包装为 422 VALIDATION_ERROR（契约 §1.2）。"""
        settings: Settings = request.app.state.settings
        req_id = request_id()
        details = []
        for err in exc.errors():
            loc = ".".join(str(part) for part in err.get("loc", ()) if part != "body")
            details.append({"field": loc or "request", "reason": "invalid_request"})
        return error_response(
            settings,
            req_id,
            get_data_version(request.app),
            ApiError(422, "VALIDATION_ERROR", "参数校验未通过", details=details),
        )

    @application.exception_handler(sqlite3.Error)
    async def _sqlite_error(request: Request, exc: sqlite3.Error) -> JSONResponse:
        """F4：SQLite 异常边界——可识别的不可用 → 503，其余（含编程错误）→ 500。"""
        settings: Settings = request.app.state.settings
        return error_response(
            settings,
            request_id(),
            get_data_version(request.app),
            map_sqlite_error(exc),
        )

    @application.exception_handler(Exception)
    async def _unhandled_error(request: Request, exc: Exception) -> JSONResponse:
        """F4：未预期异常兜底——500 脱敏信封，不暴露 SQL、路径或堆栈。"""
        settings: Settings = request.app.state.settings
        return error_response(
            settings,
            request_id(),
            get_data_version(request.app),
            InternalError(),
        )
