"""准入领域规则测试:容量分段、配额、维护冲突与批量原子性。"""

import unittest
from datetime import datetime, timedelta, timezone

from industrial_capacity.domain.admission import evaluate_batch
from industrial_capacity.domain.errors import (
    REASON_CAPACITY_EXCEEDED,
    REASON_LATENCY_CLASS_INSUFFICIENT,
    REASON_LINK_NOT_FOUND,
    REASON_MAINTENANCE_CONFLICT,
    REASON_QUOTA_BANDWIDTH_EXCEEDED,
    REASON_QUOTA_COUNT_EXCEEDED,
)
from industrial_capacity.domain.models import (
    LatencyClass,
    Link,
    MaintenanceKind,
    MaintenanceWindow,
    Reservation,
    ReservationItem,
    TenantQuota,
)

T0 = datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)


def make_link(link_id="L1", capacity=1000, latency=LatencyClass.ULTRA):
    return Link(
        link_id=link_id,
        name=f"链路{link_id}",
        capacity_mbps=capacity,
        latency_class=latency,
        reliability_target=0.999,
    )


def make_item(item_id, link_id="L1", start_offset=0, hours=2, bw=100, latency=LatencyClass.LOW, prio=1):
    return ReservationItem(
        item_id=item_id,
        link_id=link_id,
        start=T0 + timedelta(hours=start_offset),
        end=T0 + timedelta(hours=start_offset + hours),
        bandwidth_mbps=bw,
        latency_class=latency,
        business_priority=prio,
    )


def make_reservation(rid, link_id="L1", start_offset=0, hours=2, bw=100, tenant="t1"):
    return Reservation(
        reservation_id=rid,
        batch_id="b0",
        tenant_id=tenant,
        link_id=link_id,
        start=T0 + timedelta(hours=start_offset),
        end=T0 + timedelta(hours=start_offset + hours),
        bandwidth_mbps=bw,
        latency_class=LatencyClass.LOW,
        business_priority=1,
    )


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.links = {"L1": make_link()}

    def evaluate(self, items, **kwargs):
        kwargs.setdefault("tenant_id", "t1")
        kwargs.setdefault("links", self.links)
        kwargs.setdefault("maintenance", [])
        kwargs.setdefault("occupying", [])
        kwargs.setdefault("quota", None)
        return evaluate_batch(items, **kwargs)

    def test_accept_when_capacity_sufficient(self):
        accepted, rejections = self.evaluate([make_item("i1", bw=400)])
        self.assertEqual([i.item_id for i in accepted], ["i1"])
        self.assertEqual(rejections, [])

    def test_reject_unknown_link_with_reason(self):
        accepted, rejections = self.evaluate([make_item("i1", link_id="LX")])
        self.assertEqual(accepted, [])
        self.assertEqual(rejections[0].code, REASON_LINK_NOT_FOUND)

    def test_reject_latency_class_insufficient(self):
        accepted, rejections = self.evaluate([make_item("i1", latency=LatencyClass.ULTRA, bw=10)])
        # L1 为 ultra,满足;换 standard 链路则不满足
        self.assertEqual(rejections, [])
        self.links["L1"] = make_link(latency=LatencyClass.STANDARD)
        accepted, rejections = self.evaluate([make_item("i2", latency=LatencyClass.LOW, bw=10)])
        self.assertEqual(rejections[0].code, REASON_LATENCY_CLASS_INSUFFICIENT)

    def test_capacity_accounted_per_segment(self):
        # 已占用:0~2h 700M;新请求 1~3h 400M -> 1~2h 段合计 1100M 超容
        occupying = [make_reservation("r1", bw=700)]
        accepted, rejections = self.evaluate([make_item("i1", start_offset=1, bw=400)], occupying=occupying)
        self.assertEqual(rejections[0].code, REASON_CAPACITY_EXCEEDED)
        self.assertIn("已占用 700", rejections[0].message)
        # 错峰请求 2~4h 则通过
        accepted, rejections = self.evaluate([make_item("i2", start_offset=2, bw=400)], occupying=occupying)
        self.assertEqual([i.item_id for i in accepted], ["i2"])

    def test_full_outage_maintenance_conflict(self):
        maintenance = [
            MaintenanceWindow(
                window_id="mw1",
                link_id="L1",
                start=T0 + timedelta(minutes=30),
                end=T0 + timedelta(minutes=90),
                kind=MaintenanceKind.FULL_OUTAGE,
            )
        ]
        _, rejections = self.evaluate([make_item("i1")], maintenance=maintenance)
        self.assertEqual(rejections[0].code, REASON_MAINTENANCE_CONFLICT)

    def test_capacity_reduction_window_limits_capacity(self):
        maintenance = [
            MaintenanceWindow(
                window_id="mw1",
                link_id="L1",
                start=T0,
                end=T0 + timedelta(hours=4),
                kind=MaintenanceKind.CAPACITY_REDUCTION,
                available_mbps=300,
            )
        ]
        _, rejections = self.evaluate([make_item("i1", bw=400)], maintenance=maintenance)
        self.assertEqual(rejections[0].code, REASON_CAPACITY_EXCEEDED)
        accepted, _ = self.evaluate([make_item("i2", bw=300)], maintenance=maintenance)
        self.assertEqual([i.item_id for i in accepted], ["i2"])

    def test_quota_bandwidth_and_count(self):
        quota = TenantQuota(tenant_id="t1", max_mbps=150, max_active_reservations=1)
        _, rejections = self.evaluate([make_item("i1", bw=200)], quota=quota)
        self.assertEqual(rejections[0].code, REASON_QUOTA_BANDWIDTH_EXCEEDED)
        occupying = [make_reservation("r1", bw=50)]
        _, rejections = self.evaluate([make_item("i2", start_offset=1, bw=50)], quota=quota, occupying=occupying)
        self.assertEqual(rejections[0].code, REASON_QUOTA_COUNT_EXCEEDED)

    def test_batch_internal_overlap_is_detected(self):
        # 批内两条同时段各 600M,单独都合法,合并超容 -> 第二条被拒
        accepted, rejections = self.evaluate(
            [make_item("i1", bw=600), make_item("i2", bw=600)]
        )
        self.assertEqual([i.item_id for i in accepted], ["i1"])
        self.assertEqual(rejections[0].item_id, "i2")
        self.assertEqual(rejections[0].code, REASON_CAPACITY_EXCEEDED)

    def test_cross_midnight_reservation(self):
        # 跨午夜预约:22:00 ~ 次日 02:00,容量核算跨天不间断
        occupying = [
            Reservation(
                reservation_id="r-night",
                batch_id="b0",
                tenant_id="t2",
                link_id="L1",
                start=T0.replace(hour=23),
                end=T0.replace(hour=23) + timedelta(hours=2),
                bandwidth_mbps=600,
                latency_class=LatencyClass.LOW,
                business_priority=1,
            )
        ]
        item = ReservationItem(
            item_id="i-night",
            link_id="L1",
            start=T0.replace(hour=22),
            end=T0.replace(hour=22) + timedelta(hours=4),  # 22:00 ~ 次日 02:00
            bandwidth_mbps=500,
            latency_class=LatencyClass.LOW,
            business_priority=1,
        )
        _, rejections = self.evaluate([item], occupying=occupying)
        self.assertEqual(rejections[0].code, REASON_CAPACITY_EXCEEDED)
        # 400M 则整夜可容纳
        item_ok = ReservationItem(**{**item.__dict__, "item_id": "i-ok", "bandwidth_mbps": 400})
        accepted, rejections = self.evaluate([item_ok], occupying=occupying)
        self.assertEqual([i.item_id for i in accepted], ["i-ok"])
        self.assertEqual(rejections, [])


if __name__ == "__main__":
    unittest.main()
