"""观测领域服务:从遥测样本流识别连续违约序列与闭合窗口。

判定规则:
- 单样本违约:该时刻链路可用容量 < 已确认需求(预约占用合计);
- 连续窗口:违约样本按采样间隔连续出现,缺失样本(断档)会打断连续性;
- 窗口生效门槛:连续违约样本数 >= ``min_consecutive_samples``,偶发毛刺不计;
- 窗口闭合:出现健康样本、发生断档,或(结算时)当前时刻已越过末样本一个采样间隔;
- 归因:窗口与链路维护窗口相交记 MAINTENANCE(不补偿),否则记 PLATFORM。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from .identifiers import stable_id
from .models import (
    AffectedReservation,
    Attribution,
    BreachWindow,
    MaintenanceWindow,
    TelemetrySample,
)
from .timeutil import format_instant


@dataclass(frozen=True)
class ObservationConfig:
    sample_interval_minutes: int = 5
    min_consecutive_samples: int = 2


@dataclass(frozen=True)
class RawRun:
    """从样本流推导出的违约序列(可能尚未闭合)。"""

    start: datetime
    last_ts: datetime
    sample_count: int
    committed_peak_mbps: int
    worst_available_mbps: int
    closed: bool


def derive_runs(
    link_id: str,
    samples: list[TelemetrySample],
    committed_at: Callable[[datetime], int],
    config: ObservationConfig,
) -> list[RawRun]:
    """按时间顺序扫描样本,输出全部违约序列(含未闭合的末尾序列)。

    ``committed_at`` 给出任意时刻的应然需求(由应用层按历史状态还原),
    使处置动作不会扭曲违约判定。
    """
    interval = timedelta(minutes=config.sample_interval_minutes)
    ordered = sorted(samples, key=lambda s: s.ts)
    runs: list[RawRun] = []
    current: dict | None = None

    def close_current(closed: bool) -> None:
        nonlocal current
        if current is not None:
            runs.append(
                RawRun(
                    start=current["start"],
                    last_ts=current["last_ts"],
                    sample_count=current["count"],
                    committed_peak_mbps=current["peak"],
                    worst_available_mbps=current["worst"],
                    closed=closed,
                )
            )
            current = None

    for sample in ordered:
        committed = committed_at(sample.ts)
        breach = sample.available_mbps < committed
        if current is not None and sample.ts - current["last_ts"] > interval + timedelta(seconds=1):
            close_current(closed=True)  # 断档,前一序列闭合
        if breach:
            if current is None:
                current = {
                    "start": sample.ts,
                    "last_ts": sample.ts,
                    "count": 1,
                    "peak": committed,
                    "worst": sample.available_mbps,
                }
            else:
                current["last_ts"] = sample.ts
                current["count"] += 1
                current["peak"] = max(current["peak"], committed)
                current["worst"] = min(current["worst"], sample.available_mbps)
        else:
            close_current(closed=True)  # 健康样本,序列闭合
    close_current(closed=False)  # 末尾序列尚未闭合
    return runs


def run_qualifies(run: RawRun, config: ObservationConfig) -> bool:
    return run.sample_count >= config.min_consecutive_samples


def attribute_window(
    link_id: str,
    start: datetime,
    end: datetime,
    maintenance: list[MaintenanceWindow],
) -> Attribution:
    for window in maintenance:
        if window.link_id == link_id and window.start < end and window.end > start:
            return Attribution.MAINTENANCE
    return Attribution.PLATFORM


def close_run(
    link_id: str,
    run: RawRun,
    *,
    config: ObservationConfig,
    maintenance: list[MaintenanceWindow],
    affected: tuple[AffectedReservation, ...],
) -> BreachWindow:
    """把违约序列固化为闭合窗口(身份由 链路+起止 决定,重算稳定)。"""
    interval = timedelta(minutes=config.sample_interval_minutes)
    end = run.last_ts + interval
    return BreachWindow(
        window_id=stable_id("bw", link_id, format_instant(run.start), format_instant(end)),
        link_id=link_id,
        start=run.start,
        end=end,
        committed_mbps=run.committed_peak_mbps,
        worst_available_mbps=run.worst_available_mbps,
        attribution=attribute_window(link_id, run.start, end, maintenance),
        affected=affected,
    )


def window_identity(link_id: str, start: datetime, end: datetime) -> str:
    return stable_id("bw", link_id, format_instant(start), format_instant(end))
