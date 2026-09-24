"""结算领域服务:由连续观测窗口计算补偿额度,并维护台账的幂等调和。

核心不变量:
- 补偿额度 = 快照带宽 x 受影响分钟数 x (1 - 服务达成率),按账期(自然月)切分;
- 每条台账记录携带确定性 dedup_key 与内容派生 ID,
  重复结算、重复遥测、窗口重算都不会改变最终账本;
- 开放账期:调和 = 删除失效条目 + 补齐缺失条目(内容一致时完全无操作);
- 已封账账期:任何重算结果不得改写封账条目,差额以 CORRECTION_ADJUSTMENT
  调整条目计入当前开放账期,并指向被调整账期。
"""

from __future__ import annotations

from datetime import datetime

from .identifiers import stable_id
from .models import (
    Attribution,
    BreachWindow,
    LedgerEntry,
    LedgerKind,
)
from .timeutil import format_instant, minutes_between, split_by_period


def compute_credit_entries(windows: list[BreachWindow], period: str) -> list[LedgerEntry]:
    """计算指定账期应存在的违约补偿条目(仅 PLATFORM 归因产生额度)。"""
    entries: list[LedgerEntry] = []
    for window in windows:
        if window.attribution is not Attribution.PLATFORM:
            continue
        loss_ratio = 1.0 - window.service_ratio
        if loss_ratio <= 0:
            continue
        for affected in window.affected:
            overlap_start = max(window.start, affected.start)
            overlap_end = min(window.end, affected.end)
            if overlap_end <= overlap_start:
                continue
            for part_period, seg_start, seg_end in split_by_period(overlap_start, overlap_end):
                if part_period != period:
                    continue
                minutes = minutes_between(seg_start, seg_end)
                credit = round(affected.bandwidth_mbps * minutes * loss_ratio)
                if credit <= 0:
                    continue
                dedup_key = (
                    f"{LedgerKind.BREACH_CREDIT.value}|{window.window_id}|"
                    f"{affected.reservation_id}|{period}"
                )
                entries.append(
                    LedgerEntry(
                        entry_id=stable_id("le", dedup_key),
                        tenant_id=affected.tenant_id,
                        reservation_id=affected.reservation_id,
                        period=period,
                        kind=LedgerKind.BREACH_CREDIT,
                        credit_mb_minutes=credit,
                        window_start=seg_start,
                        window_end=seg_end,
                        attribution=window.attribution,
                        dedup_key=dedup_key,
                    )
                )
    return entries


def reconcile_open_period(
    desired: list[LedgerEntry],
    existing: list[LedgerEntry],
) -> tuple[list[LedgerEntry], list[LedgerEntry]]:
    """开放账期调和:返回 (待写入, 待删除)。

    按内容比对:键相同但金额等字段变化的条目会被替换(遥测更正后的
    就地重算);完全一致时不产生任何写操作(重放安全)。
    """
    existing_by_key = {e.dedup_key: e for e in existing}
    desired_keys = {e.dedup_key for e in desired}
    to_upsert = [e for e in desired if existing_by_key.get(e.dedup_key) != e]
    to_delete = [e for e in existing if e.dedup_key not in desired_keys]
    return to_upsert, to_delete


def compute_adjustments(
    desired: list[LedgerEntry],
    recorded: list[LedgerEntry],
    *,
    target_period: str,
    current_period: str,
    now: datetime,
) -> list[LedgerEntry]:
    """已封账账期的差额调整:按预约维度把差额计入当前开放账期。

    ``recorded`` 为被封账账期已有的全部相关条目(封账期内补偿条目 +
    指向该账期的历史调整条目),因此同一更正重放时差额为零、不产生新条目。
    """
    desired_by_reservation: dict[str, int] = {}
    tenant_by_reservation: dict[str, str] = {}
    for entry in desired:
        desired_by_reservation[entry.reservation_id] = (
            desired_by_reservation.get(entry.reservation_id, 0) + entry.credit_mb_minutes
        )
        tenant_by_reservation[entry.reservation_id] = entry.tenant_id

    recorded_by_reservation: dict[str, int] = {}
    recorded_count: dict[str, int] = {}
    for entry in recorded:
        recorded_by_reservation[entry.reservation_id] = (
            recorded_by_reservation.get(entry.reservation_id, 0) + entry.credit_mb_minutes
        )
        recorded_count[entry.reservation_id] = recorded_count.get(entry.reservation_id, 0) + 1
        tenant_by_reservation.setdefault(entry.reservation_id, entry.tenant_id)

    adjustments: list[LedgerEntry] = []
    for reservation_id in sorted(set(desired_by_reservation) | set(recorded_by_reservation)):
        delta = desired_by_reservation.get(reservation_id, 0) - recorded_by_reservation.get(
            reservation_id, 0
        )
        if delta == 0:
            continue
        dedup_key = (
            f"{LedgerKind.CORRECTION_ADJUSTMENT.value}|{target_period}|{reservation_id}|"
            f"{delta}|{recorded_count.get(reservation_id, 0)}"
        )
        adjustments.append(
            LedgerEntry(
                entry_id=stable_id("le", dedup_key),
                tenant_id=tenant_by_reservation[reservation_id],
                reservation_id=reservation_id,
                period=current_period,
                kind=LedgerKind.CORRECTION_ADJUSTMENT,
                credit_mb_minutes=delta,
                window_start=now,
                window_end=now,
                attribution=None,
                dedup_key=dedup_key,
                target_period=target_period,
            )
        )
    return adjustments
