"""时间线纯算法测试：区间相交、峰值占用、连续观测窗口。"""

from __future__ import annotations

import unittest
from dataclasses import dataclass

from industrial_capacity.application.timeline import (
    BUCKET_SECONDS,
    capacity_breaches,
    contiguous_degraded_buckets,
    intersect,
    minute_bucket,
    overlaps,
    peak_concurrent_usage,
    period_of,
    tenant_peak_usage,
)
from industrial_capacity.domain.values import LatencyClass, ReservationStatus


class IntervalTests(unittest.TestCase):
    def test_overlap_half_open(self) -> None:
        self.assertTrue(overlaps(0, 10, 10, 20) is False)  # 端点相接不算重叠
        self.assertTrue(overlaps(0, 10, 5, 15))
        self.assertEqual(intersect(0, 10, 5, 15), (5, 10))
        self.assertIsNone(intersect(0, 10, 10, 20))

    def test_period_of_utc_month(self) -> None:
        self.assertEqual(period_of(0), "1970-01")
        # 2025-08-31 23:59 仍属 8 月；跨午夜后属 9 月
        self.assertEqual(period_of(utc_boundary(2025, 8, 31, 23, 59)), "2025-08")
        self.assertEqual(period_of(utc_boundary(2025, 9, 1, 0, 0)), "2025-09")


def utc_boundary(y, mo, d, h, mi):
    from datetime import datetime, timezone

    return int(datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp())


class PeakUsageTests(unittest.TestCase):
    def test_peak_concurrent(self) -> None:
        intervals = [(0, 100, 30.0), (50, 150, 40.0), (150, 200, 20.0)]
        self.assertEqual(peak_concurrent_usage(intervals), 70.0)

    def test_touching_intervals_do_not_stack(self) -> None:
        intervals = [(0, 60, 50.0), (60, 120, 50.0)]
        self.assertEqual(peak_concurrent_usage(intervals), 50.0)

    def test_capacity_breaches_reports_concurrent(self) -> None:
        @dataclass
        class R:
            code: str
            current_links: list
            starts_at: int
            ends_at: int
            bandwidth_mbps: float
            is_active: bool = True
            tenant_code: str = "T"

        rs = [
            R("a", ["L1"], 0, 120, 600.0),
            R("b", ["L1"], 60, 180, 600.0),
        ]
        breaches = capacity_breaches(rs, "L1", 0, 180, 1000.0)
        self.assertEqual(len(breaches), 1)
        self.assertEqual(breaches[0]["used_mbps"], 1200.0)
        self.assertEqual({x["reservation"] for x in breaches[0]["concurrent"]}, {"a", "b"})

    def test_tenant_peak_filters_active_only(self) -> None:
        from industrial_capacity.domain.entities import Reservation

        def mk(code, status):
            return Reservation(
                code=code,
                tenant_code="T1",
                link_codes=["L"],
                starts_at=0,
                ends_at=60,
                bandwidth_mbps=100.0,
                latency_class=LatencyClass.LOW,
                reliability=0.99,
                priority=10,
                status=status,
            )

        rs = [
            mk("a", ReservationStatus.CONFIRMED),
            mk("b", ReservationStatus.ABORTED),
        ]
        self.assertEqual(tenant_peak_usage(rs, "T1"), 100.0)


@dataclass
class _Sample:
    observed: bool = True
    superseded: bool = False
    bad: bool = False


class ContiguousWindowTests(unittest.TestCase):
    def test_requires_three_consecutive_bad_buckets(self) -> None:
        t0 = minute_bucket(10_000_000)
        samples = {t0 + i * BUCKET_SECONDS: _Sample(bad=(i in (0, 1))) for i in range(5)}
        segs = contiguous_degraded_buckets(
            samples, t0, t0 + 5 * 60, lambda s: s.bad, min_consecutive=3
        )
        self.assertEqual(segs, [])  # 仅 2 分钟，不构成退化

    def test_segment_bounds(self) -> None:
        t0 = minute_bucket(10_000_000)
        samples = {t0 + i * 60: _Sample(bad=(2 <= i <= 5)) for i in range(8)}
        segs = contiguous_degraded_buckets(
            samples, t0, t0 + 8 * 60, lambda s: s.bad, min_consecutive=3
        )
        self.assertEqual(segs, [(t0 + 2 * 60, t0 + 6 * 60)])  # 4 个连续分钟

    def test_gap_breaks_continuity(self) -> None:
        t0 = minute_bucket(10_000_000)
        samples = {t0 + i * 60: _Sample(bad=True) for i in (0, 1, 3, 4, 5)}
        segs = contiguous_degraded_buckets(
            samples, t0, t0 + 6 * 60, lambda s: s.bad, min_consecutive=3
        )
        self.assertEqual(segs, [(t0 + 3 * 60, t0 + 6 * 60)])  # 缺桶切断前段

    def test_superseded_samples_ignored(self) -> None:
        t0 = minute_bucket(10_000_000)
        samples = {t0 + i * 60: _Sample(bad=True, superseded=True) for i in range(4)}
        segs = contiguous_degraded_buckets(
            samples, t0, t0 + 4 * 60, lambda s: s.bad, min_consecutive=3
        )
        self.assertEqual(segs, [])


if __name__ == "__main__":
    unittest.main()
