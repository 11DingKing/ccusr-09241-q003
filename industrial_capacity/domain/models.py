"""领域模型:链路、维护窗口、租户配额、预约、遥测、违约窗口与台账。

模型均为不可变倾向的 dataclass;状态迁移通过应用服务完成,
领域服务(准入/观测/归因/处置/结算)只依赖这些结构,不感知持久化。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class LatencyClass(str, Enum):
    """时延等级,数值越大代表等级越高(时延越低)。"""

    STANDARD = "standard"
    LOW = "low"
    ULTRA = "ultra"

    @property
    def rank(self) -> int:
        return _LATENCY_RANK[self]

    def satisfies(self, required: "LatencyClass") -> bool:
        return self.rank >= required.rank


_LATENCY_RANK = {
    LatencyClass.STANDARD: 1,
    LatencyClass.LOW: 2,
    LatencyClass.ULTRA: 3,
}


class MaintenanceKind(str, Enum):
    FULL_OUTAGE = "FULL_OUTAGE"  # 窗口期内链路完全不可用
    CAPACITY_REDUCTION = "CAPACITY_REDUCTION"  # 窗口期内容量收缩到 available_mbps


class ReservationStatus(str, Enum):
    CONFIRMED = "CONFIRMED"  # 已确认,正常占用容量
    MIGRATED = "MIGRATED"  # 已迁移到其他链路(仍占用容量)
    DEGRADED = "DEGRADED"  # 已降级(带宽被压缩)
    TERMINATED = "TERMINATED"  # 已中止,不再占用容量
    COMPLETED = "COMPLETED"  # 已到期结束

    @property
    def occupies_capacity(self) -> bool:
        return self in (
            ReservationStatus.CONFIRMED,
            ReservationStatus.MIGRATED,
            ReservationStatus.DEGRADED,
        )


class BatchStatus(str, Enum):
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"


class Attribution(str, Enum):
    """违约归因:只有平台责任会产生补偿额度。"""

    PLATFORM = "PLATFORM"  # 平台链路退化,需补偿
    MAINTENANCE = "MAINTENANCE"  # 计划内维护,不补偿(应已走处置流程)


class RemediationKind(str, Enum):
    MIGRATE = "MIGRATE"
    DEGRADE = "DEGRADE"
    TERMINATE = "TERMINATE"


class RemediationTrigger(str, Enum):
    MAINTENANCE = "MAINTENANCE"
    DEGRADATION = "DEGRADATION"


class LedgerKind(str, Enum):
    BREACH_CREDIT = "BREACH_CREDIT"  # 违约补偿额度
    CORRECTION_ADJUSTMENT = "CORRECTION_ADJUSTMENT"  # 遥测更正引发的跨期调整


class PeriodStatus(str, Enum):
    OPEN = "OPEN"
    SEALED = "SEALED"


@dataclass(frozen=True)
class Link:
    link_id: str
    name: str
    capacity_mbps: int
    latency_class: LatencyClass
    reliability_target: float  # 可靠性目标,如 0.999
    workshops: tuple[str, ...] = ()
    active: bool = True


@dataclass(frozen=True)
class MaintenanceWindow:
    window_id: str
    link_id: str
    start: datetime
    end: datetime
    kind: MaintenanceKind
    available_mbps: int = 0  # CAPACITY_REDUCTION 时的剩余容量

    def effective_capacity(self, nominal_capacity: int) -> int:
        if self.kind is MaintenanceKind.FULL_OUTAGE:
            return 0
        return min(nominal_capacity, self.available_mbps)


@dataclass(frozen=True)
class TenantQuota:
    tenant_id: str
    max_mbps: int  # 租户并发占用带宽上限
    max_active_reservations: int  # 租户并发活动预约上限


@dataclass
class Reservation:
    reservation_id: str
    batch_id: str
    tenant_id: str
    link_id: str
    start: datetime
    end: datetime
    bandwidth_mbps: int
    latency_class: LatencyClass
    business_priority: int  # 数值越大业务优先级越高
    status: ReservationStatus = ReservationStatus.CONFIRMED
    terminated_at: datetime | None = None  # 中止生效时刻(用于还原历史占用)
    version: int = 0  # 每次处置动作递增,用于并发与审计


@dataclass(frozen=True)
class ReservationItem:
    """批量预约中的单条请求。"""

    item_id: str
    link_id: str
    start: datetime
    end: datetime
    bandwidth_mbps: int
    latency_class: LatencyClass
    business_priority: int


@dataclass(frozen=True)
class ItemRejection:
    item_id: str
    code: str
    message: str


@dataclass(frozen=True)
class ReservationBatch:
    """批量预约的原子化结果:要么全部确认,要么全部拒绝并给出逐条原因。"""

    batch_id: str
    tenant_id: str
    status: BatchStatus
    reservation_ids: tuple[str, ...] = ()
    rejections: tuple[ItemRejection, ...] = ()


@dataclass(frozen=True)
class TelemetrySample:
    """一次链路遥测观测。sample_id 为幂等键,重复上报不改变结果。"""

    sample_id: str
    link_id: str
    ts: datetime
    available_mbps: int
    latency_ms: float
    loss_ratio: float
    corrects_sample_id: str | None = None  # 更正样本指向被更正的原始样本


@dataclass(frozen=True)
class AffectedReservation:
    """违约窗口确认时刻的受影响预约快照。

    快照在窗口首次确认时采集,之后处置动作(迁移/降级/中止)不会改写
    历史事实,补偿额度始终以快照为准。
    """

    reservation_id: str
    tenant_id: str
    bandwidth_mbps: int
    business_priority: int
    start: datetime  # 预约活动区间,用于把窗口裁剪到预约实际在约时段
    end: datetime


@dataclass(frozen=True)
class BreachWindow:
    """连续观测窗口:链路上一段持续不满足已确认需求的时段(已闭合)。"""

    window_id: str
    link_id: str
    start: datetime
    end: datetime
    committed_mbps: int  # 窗口内已确认需求峰值
    worst_available_mbps: int  # 窗口内观测到的最差可用容量
    attribution: Attribution
    affected: tuple[AffectedReservation, ...] = ()

    @property
    def service_ratio(self) -> float:
        if self.committed_mbps <= 0:
            return 1.0
        return max(0.0, min(1.0, self.worst_available_mbps / self.committed_mbps))


@dataclass
class BreachRun:
    """进行中的违约序列:达到最少连续样本数后确认,闭合后转为 BreachWindow。

    受影响预约快照在序列确认时采集(先于任何处置动作)。
    """

    link_id: str
    start: datetime
    last_ts: datetime
    committed_peak_mbps: int
    worst_available_mbps: int
    affected: tuple[AffectedReservation, ...]
    remediation_fired: bool = False


@dataclass(frozen=True)
class RemediationAction:
    """一次处置动作(迁移/降级/中止),dedup_key 保证重复评估不重复执行。"""

    action_id: str
    reservation_id: str
    trigger: RemediationTrigger
    kind: RemediationKind
    detail: dict
    dedup_key: str
    created_at: datetime


@dataclass(frozen=True)
class LedgerEntry:
    """台账条目。dedup_key 唯一,重复结算/重复遥测不会产生重复条目。"""

    entry_id: str
    tenant_id: str
    reservation_id: str
    period: str  # 条目归属的账期(调整条目归属当前开放账期)
    kind: LedgerKind
    credit_mb_minutes: int  # 补偿额度,单位 Mbps·分钟;调整条目可为负
    window_start: datetime
    window_end: datetime
    attribution: Attribution | None
    dedup_key: str
    target_period: str | None = None  # 调整条目指向的被调整账期


@dataclass
class SettlementPeriod:
    period: str
    status: PeriodStatus = PeriodStatus.OPEN
    run_count: int = 0  # 结算执行次数(重放安全,仅作审计)
    sealed_at: datetime | None = None
