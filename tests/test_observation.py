"""观测领域规则测试:连续窗口的识别、闭合与归因。"""

import unittest
from datetime import datetime, timedelta, timezone

from industrial_capacity.domain.admission import committed_demand_at
from industrial_capacity.domain.models import (
    LatencyClass,
    MaintenanceKind,
    MaintenanceWindow,
    Reservation,
    TelemetrySample,
)
from industrial_capacity.domain.observation import (
    ObservationConfig,
    attribute_window,
    derive_runs,
    run_qualifies,
)

T0 = datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)
CONFIG = ObservationConfig(sample_interval_minutes=5, min_consecutive_samples=2)


def sample(sid, minutes, available):
    return TelemetrySample(
        sample_id=sid,
        link_id="L1",
        ts=T0 + timedelta(minutes=minutes),
        available_mbps=available,
        latency_ms=1.0,
        loss_ratio=0.0,
    )


def reservation(rid, bw, start_min=0, hours=4):
    return Reservation(
        reservation_id=rid,
        batch_id="b",
        tenant_id="t1",
        link_id="L1",
        start=T0 + timedelta(minutes=start_min),
        end=T0 + timedelta(minutes=start_min) + timedelta(hours=hours),
        bandwidth_mbps=bw,
        latency_class=LatencyClass.LOW,
        business_priority=1,
    )


def committed_fn(reservations):
    return lambda t: committed_demand_at("L1", t, reservations)


class DeriveRunsTests(unittest.TestCase):
    def test_continuous_breach_forms_single_run(self):
        samples = [sample(f"s{i}", i * 5, 300) for i in range(4)]  # 0,5,10,15 全部违约
        runs = derive_runs("L1", samples, committed_fn([reservation("r1", 1000)]), CONFIG)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].sample_count, 4)
        self.assertEqual(runs[0].committed_peak_mbps, 1000)
        self.assertEqual(runs[0].worst_available_mbps, 300)
        self.assertFalse(runs[0].closed)  # 末尾序列未闭合

    def test_healthy_sample_closes_run(self):
        samples = [sample("s0", 0, 300), sample("s1", 5, 300), sample("s2", 10, 1000)]
        runs = derive_runs("L1", samples, committed_fn([reservation("r1", 1000)]), CONFIG)
        self.assertEqual(len(runs), 1)
        self.assertTrue(runs[0].closed)

    def test_gap_breaks_continuity(self):
        # 0,5 违约;15 分钟样本缺失;20,25 又违约 -> 两个序列
        samples = [
            sample("s0", 0, 300),
            sample("s1", 5, 300),
            sample("s2", 20, 300),
            sample("s3", 25, 300),
        ]
        runs = derive_runs("L1", samples, committed_fn([reservation("r1", 1000)]), CONFIG)
        self.assertEqual(len(runs), 2)
        self.assertTrue(runs[0].closed)

    def test_single_blip_does_not_qualify(self):
        samples = [sample("s0", 0, 300), sample("s1", 5, 1000)]
        runs = derive_runs("L1", samples, committed_fn([reservation("r1", 1000)]), CONFIG)
        self.assertEqual(len(runs), 1)
        self.assertFalse(run_qualifies(runs[0], CONFIG))  # 毛刺不足连续窗口门槛

    def test_no_breach_when_capacity_covers_demand(self):
        samples = [sample(f"s{i}", i * 5, 1000) for i in range(3)]
        runs = derive_runs("L1", samples, committed_fn([reservation("r1", 800)]), CONFIG)
        self.assertEqual(runs, [])


class AttributionTests(unittest.TestCase):
    def test_maintenance_overlap_is_not_platform_fault(self):
        maintenance = [
            MaintenanceWindow(
                window_id="mw1",
                link_id="L1",
                start=T0,
                end=T0 + timedelta(hours=1),
                kind=MaintenanceKind.CAPACITY_REDUCTION,
                available_mbps=300,
            )
        ]
        attribution = attribute_window("L1", T0 + timedelta(minutes=10), T0 + timedelta(minutes=20), maintenance)
        self.assertEqual(attribution.value, "MAINTENANCE")

    def test_no_maintenance_means_platform_fault(self):
        attribution = attribute_window("L1", T0, T0 + timedelta(minutes=20), [])
        self.assertEqual(attribution.value, "PLATFORM")


if __name__ == "__main__":
    unittest.main()
