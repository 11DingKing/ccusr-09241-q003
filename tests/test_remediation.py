"""处置领域规则测试:按业务优先级的迁移/降级/中止选择。"""

import unittest
from datetime import datetime, timedelta, timezone

from industrial_capacity.domain.models import (
    LatencyClass,
    Link,
    MaintenanceKind,
    MaintenanceWindow,
    Reservation,
)
from industrial_capacity.domain.remediation import PlannedKind, plan_remediation

T0 = datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)


def link(lid, capacity=1000, latency=LatencyClass.ULTRA, active=True):
    return Link(
        link_id=lid,
        name=lid,
        capacity_mbps=capacity,
        latency_class=latency,
        reliability_target=0.999,
        active=active,
    )


def reservation(rid, link_id="L1", bw=100, prio=1, start_offset=0, hours=4):
    return Reservation(
        reservation_id=rid,
        batch_id="b",
        tenant_id="t1",
        link_id=link_id,
        start=T0 + timedelta(hours=start_offset),
        end=T0 + timedelta(hours=start_offset + hours),
        bandwidth_mbps=bw,
        latency_class=LatencyClass.LOW,
        business_priority=prio,
    )


class RemediationPlanTests(unittest.TestCase):
    def test_migrate_preferred_when_spare_link_exists(self):
        links = {"L1": link("L1"), "L2": link("L2")}
        affected = [reservation("r1", bw=200)]
        plans = plan_remediation(
            affected,
            links=links,
            maintenance=[],
            occupying=list(affected),
            horizon_start=T0,
            capacity_override_by_link={"L1": 0},
        )
        self.assertEqual(plans[0].kind, PlannedKind.MIGRATE)
        self.assertEqual(plans[0].detail["to_link_id"], "L2")

    def test_degrade_when_no_spare_link(self):
        links = {"L1": link("L1")}
        affected = [reservation("r1", bw=500)]
        plans = plan_remediation(
            affected,
            links=links,
            maintenance=[],
            occupying=list(affected),
            horizon_start=T0,
            capacity_override_by_link={"L1": 300},
        )
        self.assertEqual(plans[0].kind, PlannedKind.DEGRADE)
        self.assertEqual(plans[0].detail["to_bandwidth_mbps"], 300)

    def test_terminate_when_capacity_is_zero(self):
        links = {"L1": link("L1")}
        affected = [reservation("r1", bw=500)]
        plans = plan_remediation(
            affected,
            links=links,
            maintenance=[],
            occupying=list(affected),
            horizon_start=T0,
            capacity_override_by_link={"L1": 0},
        )
        self.assertEqual(plans[0].kind, PlannedKind.TERMINATE)

    def test_business_priority_orders_outcomes(self):
        # 备用链路只能容纳一条:高优先级迁移,低优先级中止
        links = {"L1": link("L1"), "L2": link("L2", capacity=300)}
        high = reservation("r-high", bw=300, prio=10)
        low = reservation("r-low", bw=300, prio=1)
        plans = plan_remediation(
            [low, high],  # 故意乱序传入
            links=links,
            maintenance=[],
            occupying=[high, low],
            horizon_start=T0,
            capacity_override_by_link={"L1": 0},
        )
        by_reservation = {p.reservation_id: p for p in plans}
        self.assertEqual(by_reservation["r-high"].kind, PlannedKind.MIGRATE)
        self.assertEqual(by_reservation["r-low"].kind, PlannedKind.TERMINATE)

    def test_migration_respects_latency_class(self):
        links = {"L1": link("L1"), "L2": link("L2", latency=LatencyClass.STANDARD)}
        affected = [reservation("r1", bw=200)]  # 需要 LOW,L2 只有 STANDARD
        plans = plan_remediation(
            affected,
            links=links,
            maintenance=[],
            occupying=list(affected),
            horizon_start=T0,
            capacity_override_by_link={"L1": 100},
        )
        self.assertEqual(plans[0].kind, PlannedKind.DEGRADE)

    def test_maintenance_reduction_triggers_partial_degrade(self):
        links = {"L1": link("L1")}
        maintenance = [
            MaintenanceWindow(
                window_id="mw1",
                link_id="L1",
                start=T0,
                end=T0 + timedelta(hours=4),
                kind=MaintenanceKind.CAPACITY_REDUCTION,
                available_mbps=200,
            )
        ]
        affected = [reservation("r1", bw=500)]
        plans = plan_remediation(
            affected,
            links=links,
            maintenance=maintenance,
            occupying=list(affected),
            horizon_start=T0,
        )
        self.assertEqual(plans[0].kind, PlannedKind.DEGRADE)
        self.assertEqual(plans[0].detail["to_bandwidth_mbps"], 200)

    def test_unaffected_when_capacity_still_sufficient(self):
        links = {"L1": link("L1")}
        affected = [reservation("r1", bw=100)]
        plans = plan_remediation(
            affected,
            links=links,
            maintenance=[],
            occupying=list(affected),
            horizon_start=T0,
            capacity_override_by_link={"L1": 500},
        )
        self.assertEqual(plans[0].kind, PlannedKind.NONE)


if __name__ == "__main__":
    unittest.main()
