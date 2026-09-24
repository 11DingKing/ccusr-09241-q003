"""应用层异常：携带对外可解释的原因码与明细。"""

from __future__ import annotations


class AppError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


class NotFoundError(AppError):
    def __init__(self, message: str, details: dict | None = None):
        super().__init__("NOT_FOUND", message, details)


class ConflictError(AppError):
    def __init__(self, message: str, details: dict | None = None):
        super().__init__("CONFLICT", message, details)


class ValidationError(AppError):
    def __init__(self, message: str, details: dict | None = None, code: str = "BAD_REQUEST"):
        super().__init__(code, message, details)


class PeriodClosedError(AppError):
    def __init__(self, period: str):
        super().__init__(
            "PERIOD_CLOSED",
            f"账期 {period} 已封账，不允许写入；更正金额只能计入当前开放账期",
            {"period": period},
        )
