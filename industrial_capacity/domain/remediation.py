"""处置领域服务:维护窗口或遥测退化冲击已确认预约时的迁移/降级/中止决策。

决策规则(纯函数,结果由应用服务落库):
- 受影响预约按业务优先级降序处理(同级按 ID 升序保证确定性),
  高优先级优先占用迁移目标容量,低优先级更可能被降级或中止;
- 迁移:寻找满足时延等级、无维护冲突、剩余时段容量足够的其他在用链路,
  候选中优先选择剩余容量最大者(再按 ID 升序);
- 降级:无法迁移时,把带宽压缩到本链路剩余可用容量;
- 中止:本链路剩余可用容量为零时中止预约。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .admission import _effective_capacity, _elementary_segments
from .models import (
    Link,
    MaintenanceWindow,
    RemediationKind,
    Reservation,
)


class PlannedKind(str, Enum):
    MIGRATE = "MIGRATE"
    DEGRADE = "DEGRADE"
    TERMINATE = "TERMINATE"
    NONE = "NONE"


@dataclass(frozen=True)
class PlannedAction:
    reservation_id: str
    kind: PlannedKind
    detail: dict


def _available_on_link(
    link: Link,
    windows: list[MaintenanceWindow],
    occupying: list[Reservation],
    start: datetime,
    end: datetime,
    *,
    capacity_override: int | None = None,
    exclude_reservation_id: str | None = None,
) -> int:
    """[start, end) 内链路剩余可用容量的最小值(逐基本段核算)。"""
    link_windows = [w for w in windows if w.link_id == link.link_id]
    others = [
        r
        for r in occupying
        if r.link_id == link.link_id and r.reservation_id != exclude_reservation_id
    ]
    cuts: list[datetime] = []
    for r in others:
        cuts.extend([r.start, r.end])
    for w in link_windows:
        cuts.extend([w.start, w.end])
    best: int | None = None
    for seg_start, seg_end in _elementary_segments(start, end, cuts):
        used = sum(r.bandwidth_mbps for r in others if r.start <= seg_start and r.end >= seg_end)
        effective = _effective_capacity(link, link_windows, seg_start, seg_end)
        if capacity_override is not None:
            effective = min(effective, capacity_override)
        available = effective - used
        best = available if best is None else min(best, available)
    return best if best is not None else 0


def plan_remediation(
    affected: list[Reservation],
    *,
    links: dict[str, Link],
    maintenance: list[MaintenanceWindow],
    occupying: list[Reservation],
    horizon_start: datetime | None = None,
    horizon_end_by_reservation: dict[str, datetime] | None = None,
    capacity_override_by_link: dict[str, int] | None = None,
) -> list[PlannedAction]:
    """为受影响预约逐条制定处置计划。

    ``horizon_start`` 为规划区间起点(通常为当前时刻),早于它的历史时段
    不参与容量核算;``occupying`` 为当前全部占容量预约。规划过程中会把
    已决定的迁移/降级同步进工作副本,保证同一事件内高优先级先占位、
    低优先级感知占位结果。
    """
    working = list(occupying)
    plans: list[PlannedAction] = []
    ordered = sorted(affected, key=lambda r: (-r.business_priority, r.reservation_id))
    overrides = capacity_override_by_link or {}

    def replace_working(updated: Reservation) -> None:
        nonlocal working
        working = [u if u.reservation_id != updated.reservation_id else updated for u in working]

    for reservation in ordered:
        link = links.get(reservation.link_id)
        if link is None:
            continue
        plan_start = max(reservation.start, horizon_start) if horizon_start else reservation.start
        horizon_end = (horizon_end_by_reservation or {}).get(
            reservation.reservation_id, reservation.end
        )
        if horizon_end <= plan_start:
            continue

        # 1) 尝试迁移到其他链路
        best_choice: tuple[int, str] | None = None  # (剩余容量取负以便升序选最大, 链路ID)
        for candidate in links.values():
            if not candidate.active or candidate.link_id == link.link_id:
                continue
            if not candidate.latency_class.satisfies(reservation.latency_class):
                continue
            available = _available_on_link(
                candidate,
                maintenance,
                working,
                plan_start,
                horizon_end,
                capacity_override=overrides.get(candidate.link_id),
            )
            if available >= reservation.bandwidth_mbps:
                key = (-available, candidate.link_id)
                if best_choice is None or key < best_choice:
                    best_choice = key
        if best_choice is not None:
            target_id = best_choice[1]
            moved = Reservation(
                reservation_id=reservation.reservation_id,
                batch_id=reservation.batch_id,
                tenant_id=reservation.tenant_id,
                link_id=target_id,
                start=reservation.start,
                end=reservation.end,
                bandwidth_mbps=reservation.bandwidth_mbps,
                latency_class=reservation.latency_class,
                business_priority=reservation.business_priority,
                status=reservation.status,
                terminated_at=reservation.terminated_at,
                version=reservation.version,
            )
            replace_working(moved)
            plans.append(
                PlannedAction(
                    reservation.reservation_id,
                    PlannedKind.MIGRATE,
                    {
                        "from_link_id": link.link_id,
                        "to_link_id": target_id,
                        "bandwidth_mbps": reservation.bandwidth_mbps,
                    },
                )
            )
            continue

        # 2) 本链路降级
        available = _available_on_link(
            link,
            maintenance,
            working,
            plan_start,
            horizon_end,
            capacity_override=overrides.get(link.link_id),
            exclude_reservation_id=reservation.reservation_id,
        )
        new_bandwidth = min(reservation.bandwidth_mbps, max(0, available))
        if new_bandwidth <= 0:
            plans.append(
                PlannedAction(
                    reservation.reservation_id,
                    PlannedKind.TERMINATE,
                    {"link_id": link.link_id, "reason": "链路剩余可用容量为零"},
                )
            )
        elif new_bandwidth < reservation.bandwidth_mbps:
            degraded = Reservation(
                reservation_id=reservation.reservation_id,
                batch_id=reservation.batch_id,
                tenant_id=reservation.tenant_id,
                link_id=reservation.link_id,
                start=reservation.start,
                end=reservation.end,
                bandwidth_mbps=new_bandwidth,
                latency_class=reservation.latency_class,
                business_priority=reservation.business_priority,
                status=reservation.status,
                terminated_at=reservation.terminated_at,
                version=reservation.version,
            )
            replace_working(degraded)
            plans.append(
                PlannedAction(
                    reservation.reservation_id,
                    PlannedKind.DEGRADE,
                    {
                        "link_id": link.link_id,
                        "from_bandwidth_mbps": reservation.bandwidth_mbps,
                        "to_bandwidth_mbps": new_bandwidth,
                    },
                )
            )
        else:
            plans.append(PlannedAction(reservation.reservation_id, PlannedKind.NONE, {}))
    return plans


def to_remediation_kind(kind: PlannedKind) -> RemediationKind:
    return {
        PlannedKind.MIGRATE: RemediationKind.MIGRATE,
        PlannedKind.DEGRADE: RemediationKind.DEGRADE,
        PlannedKind.TERMINATE: RemediationKind.TERMINATE,
    }[kind]
