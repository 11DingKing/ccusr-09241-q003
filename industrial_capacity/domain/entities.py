"""领域实体：链路、租户、维护窗口、预约、遥测、事件、动作、补偿、账本、账期。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .values import (
    ActionKind,
    CompensationStatus,
    EntryType,
    Impact,
    IncidentKind,
    IncidentStatus,
    LatencyClass,
    LinkStatus,
    PeriodStatus,
    Priority,
    ReservationStatus,
)


@dataclass
class Link:
    """园区专网链路（逻辑链路，可对应一段物理或切片资源）。"""

    code: str
    name: str
    total_capacity_mbps: float
    supported_latency: LatencyClass
    reliability_target: float  # 如 0.999
    status: LinkStatus = LinkStatus.ACTIVE
    alternative_codes: list[str] = field(default_factory=list)

    def supports(
        self, latency: LatencyClass, reliability: float, at_status: LinkStatus
    ) -> tuple[bool, str | None]:
        """是否满足业务对时延等级与可靠性目标的要求。"""
        if at_status is LinkStatus.FAILED:
            return False, "链路处于故障状态"
        order = [
            LatencyClass.BEST_EFFORT,
            LatencyClass.MEDIUM,
            LatencyClass.LOW,
            LatencyClass.ULTRA,
        ]
        if order.index(latency) > order.index(self.supported_latency):
            return False, (
                f"链路最高支持 {self.supported_latency.label}，"
                f"不满足 {latency.label}"
            )
        if reliability > self.reliability_target + 1e-12:
            return False, (
                f"链路可靠性目标 {self.reliability_target:.4f}，"
                f"不满足申请的 {reliability:.4f}"
            )
        return True, None


@dataclass
class Tenant:
    """接入租户（车间/业务单元）及其配额。"""

    code: str
    name: str
    quota_mbps: float  # 任一时点并发预留总带宽上限


@dataclass
class MaintenanceWindow:
    """计划维护窗口：半开区间 [starts_at, ends_at)。"""

    code: str
    link_code: str
    starts_at: int
    ends_at: int
    note: str = ""


@dataclass
class Reservation:
    """能力预约：沿一条路径（有序链路）在时间窗口内预留带宽。"""

    code: str
    tenant_code: str
    link_codes: list[str]
    starts_at: int
    ends_at: int
    bandwidth_mbps: float
    latency_class: LatencyClass
    reliability: float
    priority: Priority
    status: ReservationStatus = ReservationStatus.CONFIRMED
    batch_code: str | None = None
    # 当前实际承载路径（迁移后改变）与实际时延等级（降级后改变）
    current_links: list[str] = field(default_factory=list)
    current_latency: LatencyClass | None = None
    created_at: int = 0

    def __post_init__(self) -> None:
        if not self.current_links:
            self.current_links = list(self.link_codes)
        if self.current_latency is None:
            self.current_latency = self.latency_class

    @property
    def is_active(self) -> bool:
        return self.status in (
            ReservationStatus.CONFIRMED,
            ReservationStatus.MIGRATED,
            ReservationStatus.DEGRADED,
        )

    def overlaps(self, starts_at: int, ends_at: int) -> bool:
        return self.starts_at < ends_at and starts_at < self.ends_at


@dataclass
class TelemetrySample:
    """链路遥测样本：[ts, ts+60s) 一分钟桶内的聚合观测。

    observed=True 为平台真实观测；更正时以新版本覆盖，旧版本保留可审计。
    """

    link_code: str
    bucket_ts: int  # 桶起始时间（分钟对齐）
    observed_latency_ms: float | None  # None 表示该分钟无观测（数据缺口）
    observed_reliability: float | None
    observed: bool
    version: int = 1
    superseded: bool = False


@dataclass
class Incident:
    """运行事件：链路故障、维护占用或遥测退化。

    维护与故障由系统登记直接产生；遥测退化由连续观测窗口检测产生，
    更正遥测后事件可被撤销（REVOKED），其下游补偿随之冲销。
    """

    code: str
    kind: IncidentKind
    link_code: str
    starts_at: int
    ends_at: int | None  # OPEN 事件为 None
    status: IncidentStatus = IncidentStatus.OPEN
    note: str = ""
    evidence: list[str] = field(default_factory=list)
    created_at: int = 0
    closed_at: int | None = None


@dataclass
class ReservationAction:
    """事件处置动作：对受影响预约执行迁移/降级/中止，并记录归因。"""

    code: str
    incident_code: str
    reservation_code: str
    action: ActionKind
    decided_at: int
    impact: Impact
    effective_from: int
    effective_to: int | None  # 事件关闭时收口
    reason: str
    from_links: list[str] = field(default_factory=list)
    to_links: list[str] = field(default_factory=list)
    from_latency: LatencyClass | None = None
    to_latency: LatencyClass | None = None


@dataclass
class Compensation:
    """一笔补偿计算结果：归因到具体事件与预约，依据连续观测窗口。

    幂等键 (incident_code, reservation_code) 唯一；重放返回同一笔，
    更正后旧版本置 SUPERSEDED，按新版本差额调整。
    """

    code: str
    incident_code: str
    reservation_code: str
    tenant_code: str
    amount: float
    degraded_minutes: int
    down_minutes: int
    window_start: int
    window_end: int
    attribution: str
    idempotency_key: str
    version: int = 1
    status: CompensationStatus = CompensationStatus.COMPUTED
    ledger_entry_code: str | None = None
    created_at: int = 0


@dataclass
class LedgerEntry:
    """账本分录。COMPENSATION 为正，REVERSAL 为负，ADJUSTMENT 带符号。

    同一幂等键的过账只发生一次（唯一约束），重复结算不改变账本。
    """

    code: str
    period: str  # YYYY-MM，按事件归属月份
    tenant_code: str
    entry_type: EntryType
    amount: float
    idempotency_key: str
    compensation_code: str
    created_at: int
    note: str = ""


@dataclass
class AccountingPeriod:
    """结算账期：封账后拒绝一切写入，更正只能进入当前开放账期。"""

    period: str
    status: PeriodStatus = PeriodStatus.OPEN
    closed_at: int | None = None


@dataclass
class BatchResult:
    """批量预约的原子化准入结果。"""

    code: str
    status: str  # BatchStatus
    accepted: list[str] = field(default_factory=list)  # reservation codes
    rejected: list[dict] = field(default_factory=list)  # 每项含序号与原因
    decided_at: int = 0
