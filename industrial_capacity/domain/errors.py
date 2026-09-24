"""领域错误与可解释的拒绝原因码。

准入拒绝原因码面向网络运营经理可读:每个原因都说明"哪一条、为什么、差多少"。
"""

from __future__ import annotations


class DomainError(Exception):
    """领域规则违例。"""

    code = "DOMAIN_ERROR"

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(DomainError):
    code = "NOT_FOUND"


class ConflictError(DomainError):
    code = "CONFLICT"


class PeriodSealedError(ConflictError):
    code = "PERIOD_SEALED"


class ValidationError(DomainError):
    code = "VALIDATION"


# ---- 准入拒绝原因码(可解释拒绝) ----
REASON_LINK_NOT_FOUND = "LINK_NOT_FOUND"
REASON_LINK_INACTIVE = "LINK_INACTIVE"
REASON_INVALID_TIME_RANGE = "INVALID_TIME_RANGE"
REASON_INVALID_BANDWIDTH = "INVALID_BANDWIDTH"
REASON_LATENCY_CLASS_INSUFFICIENT = "LATENCY_CLASS_INSUFFICIENT"
REASON_MAINTENANCE_CONFLICT = "MAINTENANCE_CONFLICT"
REASON_CAPACITY_EXCEEDED = "CAPACITY_EXCEEDED"
REASON_QUOTA_BANDWIDTH_EXCEEDED = "QUOTA_BANDWIDTH_EXCEEDED"
REASON_QUOTA_COUNT_EXCEEDED = "QUOTA_COUNT_EXCEEDED"
