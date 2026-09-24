"""时间线与容量占用的纯算法。

所有时间均为整数秒 Unix 时间；窗口一律采用半开区间 [start, end)。
"""

from __future__ import annotations

from datetime import datetime, timezone

BUCKET_SECONDS = 60  # 遥测桶粒度：一分钟


def period_of(ts: int) -> str:
    """事件时间所属结算账期（UTC YYYY-MM）。"""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m")


def minute_bucket(ts: int) -> int:
    """向下对齐到分钟桶起点。"""
    return ts - (ts % BUCKET_SECONDS)


def overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start < b_end and b_start < a_end


def intersect(a_start: int, a_end: int, b_start: int, b_end: int) -> tuple[int, int] | None:
    start = max(a_start, b_start)
    end = min(a_end, b_end)
    return (start, end) if start < end else None


def clamp_interval(
    start: int, end: int, lower: int, upper: int | None
) -> tuple[int, int] | None:
    """把区间裁剪进 [lower, upper)；upper 为 None 时只裁剪下界。"""
    s = max(start, lower)
    e = min(end, upper) if upper is not None else end
    return (s, e) if s < e else None


def peak_concurrent_usage(intervals: list[tuple[int, int, float]]) -> float:
    """一组 (start, end, bandwidth) 的最大并发占用量。

    扫描所有起点：某区间起点处，与它重叠的区间恰好全部生效（半开区间语义）。
    """
    peak = 0.0
    for s, _e, _b in intervals:
        used = sum(b for i_s, i_e, b in intervals if i_s <= s < i_e)
        peak = max(peak, used)
    return peak


def capacity_breaches(
    reservations,
    link_code: str,
    window_start: int,
    window_end: int,
    capacity_mbps: float,
) -> list[dict]:
    """检查指定链路在窗口内是否存在容量超限，返回每个超限点的可解释明细。

    以每条在窗口内生效预约的起点为检查时刻，报告该时刻全部并发占用。
    """
    active: list[tuple[int, int, float, str]] = []
    for r in reservations:
        if not r.is_active or link_code not in r.current_links:
            continue
        part = clamp_interval(r.starts_at, r.ends_at, window_start, window_end)
        if part:
            active.append((part[0], part[1], r.bandwidth_mbps, r.code))

    breaches: list[dict] = []
    seen_times: set[int] = set()
    for s, _e, _b, _code in sorted(active):
        if s in seen_times:
            continue
        seen_times.add(s)
        concurrent = [
            {"reservation": code, "bandwidth_mbps": b}
            for i_s, i_e, b, code in active
            if i_s <= s < i_e
        ]
        used = sum(item["bandwidth_mbps"] for item in concurrent)
        if used > capacity_mbps + 1e-9:
            breaches.append(
                {
                    "at": s,
                    "used_mbps": round(used, 6),
                    "capacity_mbps": capacity_mbps,
                    "concurrent": sorted(concurrent, key=lambda x: x["reservation"]),
                }
            )
    return breaches


def tenant_peak_usage(reservations, tenant_code: str) -> float:
    """租户在其全部已确认预约上的任一时点最大并发预留量。"""
    intervals = [
        (r.starts_at, r.ends_at, r.bandwidth_mbps)
        for r in reservations
        if r.is_active and r.tenant_code == tenant_code
    ]
    return peak_concurrent_usage(intervals)


def contiguous_degraded_buckets(
    samples: dict[int, object],
    window_start: int,
    window_end: int,
    is_breach,
    *,
    min_consecutive: int,
) -> list[tuple[int, int]]:
    """在 [window_start, window_end) 内找出连续达标的退化桶段。

    samples: bucket_ts -> TelemetrySample（可能含已失效版本）。
    is_breach(sample) -> bool：该分钟是否违反 SLO。
    数据缺口（无样本或 observed=False）不计为退化，也不打断连续性的判定，
    仅按"实际观测到违规的连续分钟数"计——缺口会切断连续性，避免臆断。

    返回若干半开秒级区间 [seg_start, seg_end)。
    """
    first = minute_bucket(window_start)
    segments: list[tuple[int, int]] = []
    run: list[int] = []

    def flush(run_buckets: list[int]) -> None:
        if len(run_buckets) >= min_consecutive:
            seg_start = max(run_buckets[0], window_start)
            seg_end = min(run_buckets[-1] + BUCKET_SECONDS, window_end)
            if seg_start < seg_end:
                segments.append((seg_start, seg_end))

    ts = first
    while ts < window_end:
        sample = samples.get(ts)
        breach = bool(
            sample and sample.observed and not sample.superseded and is_breach(sample)
        )
        if breach:
            run.append(ts)
        else:
            # 正常观测或数据缺口都显式结束本段；缺口不臆测为违约
            flush(run)
            run = []
        ts += BUCKET_SECONDS
    flush(run)
    return segments
