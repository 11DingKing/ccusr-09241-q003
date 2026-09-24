"""领域枚举与拒绝原因码。

所有枚举值都使用稳定字符串/整数，便于序列化和对外解释。
"""

from __future__ import annotations

import enum


class Priority(enum.IntEnum):
    """业务优先级，数值越大优先级越高。

    控制流量时延最敏感、关系产线安全，故最高；质检次之；设备协同再次之。
    """

    CONTROL = 40  # 控制
    QUALITY = 30  # 质检
    COLLABORATION = 20  # 设备协同
    STANDARD = 10  # 普通

    @property
    def label(self) -> str:
        return {
            Priority.CONTROL: "控制",
            Priority.QUALITY: "质检",
            Priority.COLLABORATION: "设备协同",
            Priority.STANDARD: "普通",
        }[self]

    @property
    def degradable(self) -> bool:
        """控制类预约不可降级，只能迁移或中止。"""
        return self is not Priority.CONTROL


class LatencyClass(str, enum.Enum):
    """时延等级及其最大可接受时延（毫秒）。"""

    ULTRA = "ultra"  # 超低时延
    LOW = "low"  # 低时延
    MEDIUM = "medium"  # 常规
    BEST_EFFORT = "best_effort"  # 尽力而为（降级后）

    @property
    def max_latency_ms(self) -> float | None:
        return {
            LatencyClass.ULTRA: 5.0,
            LatencyClass.LOW: 20.0,
            LatencyClass.MEDIUM: 50.0,
            LatencyClass.BEST_EFFORT: None,
        }[self]

    @property
    def label(self) -> str:
        return {
            LatencyClass.ULTRA: "超低时延(≤5ms)",
            LatencyClass.LOW: "低时延(≤20ms)",
            LatencyClass.MEDIUM: "常规(≤50ms)",
            LatencyClass.BEST_EFFORT: "尽力而为",
        }[self]

    def downgrade(self) -> "LatencyClass":
        """降级一档，已是最低档时保持不变。"""
        order = [
            LatencyClass.ULTRA,
            LatencyClass.LOW,
            LatencyClass.MEDIUM,
            LatencyClass.BEST_EFFORT,
        ]
        idx = order.index(self)
        return order[min(idx + 1, len(order) - 1)]


class LinkStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"  # 正常
    MAINTENANCE = "MAINTENANCE"  # 维护中
    FAILED = "FAILED"  # 故障


class ReservationStatus(str, enum.Enum):
    CONFIRMED = "CONFIRMED"  # 已确认、占用容量
    MIGRATED = "MIGRATED"  # 已迁移到替代路径
    DEGRADED = "DEGRADED"  # 已降级运行
    ABORTED = "ABORTED"  # 已中止
    COMPLETED = "COMPLETED"  # 窗口结束自然完成


class BatchStatus(str, enum.Enum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class IncidentKind(str, enum.Enum):
    FAILURE = "FAILURE"  # 链路硬故障
    MAINTENANCE = "MAINTENANCE"  # 维护窗口占用
    TELEMETRY_DEGRADATION = "TELEMETRY_DEGRADATION"  # 遥测 SLA 退化


class IncidentStatus(str, enum.Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    REVOKED = "REVOKED"  # 经更正回溯，事件不再成立


class ActionKind(str, enum.Enum):
    MIGRATED = "MIGRATED"
    DEGRADED = "DEGRADED"
    ABORTED = "ABORTED"


class Impact(str, enum.Enum):
    NONE = "NONE"  # 成功迁移、无中断
    DEGRADED = "DEGRADED"
    ABORTED = "ABORTED"


class CompensationStatus(str, enum.Enum):
    COMPUTED = "COMPUTED"  # 已计算、未入账
    POSTED = "POSTED"  # 已写入账本
    SUPERSEDED = "SUPERSEDED"  # 被更正计算取代


class EntryType(str, enum.Enum):
    COMPENSATION = "COMPENSATION"  # 补偿（正）
    REVERSAL = "REVERSAL"  # 冲销（负）
    ADJUSTMENT = "ADJUSTMENT"  # 跨期更正调整（带符号）


class PeriodStatus(str, enum.Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class RejectCode:
    """可解释的准入拒绝原因码。"""

    WINDOW_INVALID = "WINDOW_INVALID"
    TENANT_NOT_FOUND = "TENANT_NOT_FOUND"
    EMPTY_PATH = "EMPTY_PATH"
    LINK_NOT_FOUND = "LINK_NOT_FOUND"
    LINK_FAILED = "LINK_FAILED"
    LATENCY_UNSUPPORTED = "LATENCY_UNSUPPORTED"
    RELIABILITY_UNSUPPORTED = "RELIABILITY_UNSUPPORTED"
    MAINTENANCE_CONFLICT = "MAINTENANCE_CONFLICT"
    INSUFFICIENT_CAPACITY = "INSUFFICIENT_CAPACITY"
    TENANT_QUOTA_EXCEEDED = "TENANT_QUOTA_EXCEEDED"
    BAD_REQUEST = "BAD_REQUEST"
    CONFLICT = "CONFLICT"
    NOT_FOUND = "NOT_FOUND"
    PERIOD_CLOSED = "PERIOD_CLOSED"
