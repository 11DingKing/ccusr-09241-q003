"""准入领域服务:对批量预约做原子化容量/配额/维护窗口校验。

纯函数实现,不感知持久化;调用方(应用服务)负责取数、加锁与落库。
批量语义为全有或全无:任一 item 失败则整批拒绝,且每条失败都给出
可读原因码与差值细节,便于运营经理在排产确认前调整计划。
"""

from __future__ import annotations

from datetime import datetime

from .errors import (
    REASON_CAPACITY_EXCEEDED,
    REASON_INVALID_BANDWIDTH,
    REASON_INVALID_TIME_RANGE,
    REASON_LATENCY_CLASS_INSUFFICIENT,
    REASON_LINK_INACTIVE,
    REASON_LINK_NOT_FOUND,
    REASON_MAINTENANCE_CONFLICT,
    REASON_QUOTA_BANDWIDTH_EXCEEDED,
    REASON_QUOTA_COUNT_EXCEEDED,
)
from .models import (
    ItemRejection,
    Link,
    MaintenanceKind,
    MaintenanceWindow,
    Reservation,
    ReservationItem,
    TenantQuota,
)


def _elementary_segments(start: datetime, end: datetime, cuts: list[datetime]) -> list[tuple[datetime, datetime]]:
    """把 [start, end) 按切点分成基本段,每段内占用/容量恒定。"""
    bounds = sorted({start, end} | {c for c in cuts if start < c < end})
    return [(a, b) for a, b in zip(bounds, bounds[1:])]


def _covers(r_start: datetime, r_end: datetime, seg_start: datetime, seg_end: datetime) -> bool:
    return r_start <= seg_start and r_end >= seg_end


def _effective_capacity(
    link: Link,
    windows: list[MaintenanceWindow],
    seg_start: datetime,
    seg_end: datetime,
) -> int:
    capacity = link.capacity_mbps
    for window in windows:
        if window.start < seg_end and window.end > seg_start:
            capacity = min(capacity, window.effective_capacity(link.capacity_mbps))
    return capacity


def check_item(
    item: ReservationItem,
    *,
    tenant_id: str,
    links: dict[str, Link],
    maintenance: list[MaintenanceWindow],
    occupying: list[Reservation],
    quota: TenantQuota | None,
) -> ItemRejection | None:
    """校验单条预约请求;通过返回 None,否则返回可解释的拒绝原因。

    ``occupying`` 必须包含所有占容量的预约(含本批次已接受的条目),
    从而保证批量内部的自洽与并发下的原子判定。
    """
    link = links.get(item.link_id)
    if link is None:
        return ItemRejection(item.item_id, REASON_LINK_NOT_FOUND, f"链路 {item.link_id} 不存在")
    if not link.active:
        return ItemRejection(item.item_id, REASON_LINK_INACTIVE, f"链路 {link.name} 已停用")
    if item.end <= item.start:
        return ItemRejection(item.item_id, REASON_INVALID_TIME_RANGE, "结束时间必须晚于开始时间")
    if item.bandwidth_mbps <= 0:
        return ItemRejection(item.item_id, REASON_INVALID_BANDWIDTH, "带宽必须为正整数")
    if not link.latency_class.satisfies(item.latency_class):
        return ItemRejection(
            item.item_id,
            REASON_LATENCY_CLASS_INSUFFICIENT,
            f"链路时延等级 {link.latency_class.value} 不满足所需 {item.latency_class.value}",
        )

    link_windows = [w for w in maintenance if w.link_id == link.link_id]
    for window in link_windows:
        if window.kind is MaintenanceKind.FULL_OUTAGE and window.start < item.end and window.end > item.start:
            return ItemRejection(
                item.item_id,
                REASON_MAINTENANCE_CONFLICT,
                f"与链路维护窗口 {window.window_id} 冲突(该时段链路停用)",
            )

    # ---- 链路容量:按基本段核算 ----
    link_reservations = [r for r in occupying if r.link_id == link.link_id]
    cuts: list[datetime] = []
    for r in link_reservations:
        cuts.extend([r.start, r.end])
    for w in link_windows:
        cuts.extend([w.start, w.end])
    for seg_start, seg_end in _elementary_segments(item.start, item.end, cuts):
        used = sum(
            r.bandwidth_mbps for r in link_reservations if _covers(r.start, r.end, seg_start, seg_end)
        )
        effective = _effective_capacity(link, link_windows, seg_start, seg_end)
        if used + item.bandwidth_mbps > effective:
            return ItemRejection(
                item.item_id,
                REASON_CAPACITY_EXCEEDED,
                (
                    f"时段 {seg_start.isoformat()}~{seg_end.isoformat()} 可用容量不足:"
                    f"有效容量 {effective} Mbps,已占用 {used} Mbps,"
                    f"本请求 {item.bandwidth_mbps} Mbps"
                ),
            )

    # ---- 租户配额:并发带宽与并发条数 ----
    if quota is not None:
        tenant_reservations = [r for r in occupying if r.tenant_id == tenant_id]
        q_cuts: list[datetime] = []
        for r in tenant_reservations:
            q_cuts.extend([r.start, r.end])
        for seg_start, seg_end in _elementary_segments(item.start, item.end, q_cuts):
            concurrent = [r for r in tenant_reservations if _covers(r.start, r.end, seg_start, seg_end)]
            tenant_used = sum(r.bandwidth_mbps for r in concurrent)
            if tenant_used + item.bandwidth_mbps > quota.max_mbps:
                return ItemRejection(
                    item.item_id,
                    REASON_QUOTA_BANDWIDTH_EXCEEDED,
                    (
                        f"租户并发带宽超限:配额 {quota.max_mbps} Mbps,"
                        f"时段 {seg_start.isoformat()}~{seg_end.isoformat()} 已占用"
                        f" {tenant_used} Mbps,本请求 {item.bandwidth_mbps} Mbps"
                    ),
                )
            if len(concurrent) + 1 > quota.max_active_reservations:
                return ItemRejection(
                    item.item_id,
                    REASON_QUOTA_COUNT_EXCEEDED,
                    (
                        f"租户并发预约条数超限:配额 {quota.max_active_reservations} 条,"
                        f"该时段已有 {len(concurrent)} 条"
                    ),
                )
    return None


def evaluate_batch(
    items: list[ReservationItem],
    *,
    tenant_id: str,
    links: dict[str, Link],
    maintenance: list[MaintenanceWindow],
    occupying: list[Reservation],
    quota: TenantQuota | None,
) -> tuple[list[ReservationItem], list[ItemRejection]]:
    """原子化评估整批请求。

    返回 (可接受条目, 拒绝原因列表)。调用方约定:只要拒绝列表非空,
    整批不得落库。评估按请求顺序进行,已接受条目会参与后续条目的
    容量与配额核算,保证批量内部不自我挤占。
    """
    accepted: list[ReservationItem] = []
    rejections: list[ItemRejection] = []
    working = list(occupying)
    for item in items:
        rejection = check_item(
            item,
            tenant_id=tenant_id,
            links=links,
            maintenance=maintenance,
            occupying=working,
            quota=quota,
        )
        if rejection is not None:
            rejections.append(rejection)
            continue
        accepted.append(item)
        working.append(
            Reservation(
                reservation_id=f"pending:{item.item_id}",
                batch_id="",
                tenant_id=tenant_id,
                link_id=item.link_id,
                start=item.start,
                end=item.end,
                bandwidth_mbps=item.bandwidth_mbps,
                latency_class=item.latency_class,
                business_priority=item.business_priority,
            )
        )
    return accepted, rejections


def committed_demand_at(
    link_id: str,
    instant: datetime,
    occupying: list[Reservation],
) -> int:
    """某链路在某时刻的已确认需求(供观测窗口判定使用)。"""
    return sum(
        r.bandwidth_mbps
        for r in occupying
        if r.link_id == link_id and r.start <= instant < r.end
    )
