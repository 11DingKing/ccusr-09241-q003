"""时间工具:统一使用带时区的 UTC 时间,并提供账期(自然月)划分能力。

所有领域逻辑只接受 ``datetime`` 且必须 ``tzinfo=utc``;接口层负责解析。
账期(period)按自然月划分,格式为 ``YYYY-MM``,用于结算与封账。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .errors import ValidationError

UTC = timezone.utc

_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def require_period(period: str) -> str:
    """校验账期格式 ``YYYY-MM``;非法格式抛出 ValidationError。"""
    if not _PERIOD_RE.match(period):
        raise ValidationError(f"账期格式非法: {period!r},应为 YYYY-MM")
    return period


def parse_instant(text: str) -> datetime:
    """解析 ISO-8601 时间字符串,缺省时区按 UTC 处理。"""
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def format_instant(value: datetime) -> str:
    """输出稳定的 ISO-8601 表示(UTC,秒级精度)。"""
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def require_utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field} 必须携带时区")
    return value.astimezone(UTC)


def period_of(instant: datetime) -> str:
    """返回某个时刻所属的自然月账期,如 ``2026-09``。"""
    instant = instant.astimezone(UTC)
    return f"{instant.year:04d}-{instant.month:02d}"


def period_bounds(period: str) -> tuple[datetime, datetime]:
    """返回账期的 [start, end) 边界。"""
    year, month = (int(part) for part in period.split("-"))
    start = datetime(year, month, 1, tzinfo=UTC)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=UTC)
    else:
        end = datetime(year, month + 1, 1, tzinfo=UTC)
    return start, end


def clip_to_period(start: datetime, end: datetime, period: str) -> tuple[datetime, datetime] | None:
    """把区间裁剪到账期内;不相交时返回 None。"""
    p_start, p_end = period_bounds(period)
    lo, hi = max(start, p_start), min(end, p_end)
    if lo >= hi:
        return None
    return lo, hi


def split_by_period(start: datetime, end: datetime) -> list[tuple[str, datetime, datetime]]:
    """把 [start, end) 按自然月切分,用于跨月(跨午夜)窗口的账期归属。"""
    if end <= start:
        return []
    parts: list[tuple[str, datetime, datetime]] = []
    cursor = start
    while cursor < end:
        period = period_of(cursor)
        _, p_end = period_bounds(period)
        seg_end = min(end, p_end)
        parts.append((period, cursor, seg_end))
        cursor = seg_end
    return parts


def overlap_seconds(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> float:
    """两个区间的重叠秒数,不相交为 0。"""
    lo, hi = max(a_start, b_start), min(a_end, b_end)
    if lo >= hi:
        return 0.0
    return (hi - lo).total_seconds()


def minutes_between(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 60.0


def add_minutes(instant: datetime, minutes: float) -> datetime:
    return instant + timedelta(minutes=minutes)
