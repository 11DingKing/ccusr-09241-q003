"""运维处置测试：按优先级的迁移/降级/中止、跨午夜维护、故障恢复。"""

from __future__ import annotations

import unittest

from industrial_capacity.application.platform import Platform
from industrial_capacity.domain.values import (
    ActionKind,
    Impact,
    IncidentKind,
    ReservationStatus,
)

from _support import utc


def reserve(p, tenant, link, start, end, bw, latency="low", rel=0.99, priority="QUALITY"):
    result = p.admission.submit_batch(
        [
            {
                "tenant_code": tenant,
                "link_codes": [link],
                "starts_at": start,
                "ends_at": end,
                "bandwidth_mbps": bw,
                "latency_class": latency,
                "reliability": rel,
                "priority": priority,
            }
        ]
    )
    assert result.status == "ACCEPTED", result.rejected
    return result.accepted[0]


class FailureHandlingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.p = Platform(utc(2025, 9, 1))
        # 主链路 + 满足 SLO 的备链路
        self.p.catalog.register_link("L1", "主链路", 1000.0, "low", 0.999, ["L2"])
        self.p.catalog.register_link("L2", "备链路", 1000.0, "low", 0.999)
        self.p.catalog.register_tenant("QA", "质检车间", 5000.0)
        self.p.catalog.register_tenant("CTL", "控制车间", 5000.0)

    def test_high_priority_gets_alternative_first(self) -> None:
        t0 = utc(2025, 9, 2, 10)
        # L2 容量 600：控制 400 与质检 400 只有一条能迁移
        self.p.repo.get_link("L2").total_capacity_mbps = 600.0
        r_ctl = reserve(self.p, "CTL", "L1", t0, t0 + 3600, 400.0, priority="CONTROL")
        r_qa = reserve(self.p, "QA", "L1", t0, t0 + 3600, 400.0, priority="QUALITY")
        inc = self.p.operations.report_failure("L1", t0 + 600, t0 + 1800)

        ctl = self.p.repo.get_reservation(r_ctl)
        qa = self.p.repo.get_reservation(r_qa)
        self.assertEqual(ctl.status, ReservationStatus.MIGRATED)
        self.assertIn("L2", ctl.current_links)
        # 质检类可降级与否：硬故障链路不可用，备选容量被占 → 中止
        self.assertEqual(qa.status, ReservationStatus.ABORTED)

        actions = {a.reservation_code: a for a in self.p.repo.list_actions(inc.code)}
        self.assertEqual(actions[r_ctl].impact, Impact.NONE)
        self.assertEqual(actions[r_qa].action, ActionKind.ABORTED)

    def test_migrated_reservation_has_no_compensation_impact(self) -> None:
        t0 = utc(2025, 9, 2, 10)
        code = reserve(self.p, "QA", "L1", t0, t0 + 3600, 100.0)
        inc = self.p.operations.report_failure("L1", t0 + 600, t0 + 1200)
        action = self.p.repo.list_actions(inc.code)[0]
        self.assertEqual(action.impact, Impact.NONE)
        out = self.p.settlement.settle_incident(inc.code)
        self.assertEqual(out["postings"], [])  # 成功迁移不补偿

    def test_control_cannot_degrade_on_telemetry_event(self) -> None:
        t0 = utc(2025, 9, 3, 8)
        code = reserve(
            self.p, "CTL", "L1", t0, t0 + 1800, 100.0,
            latency="low", rel=0.99, priority="CONTROL",
        )
        # 无备链路（去掉 L1 的替代）：控制类只能中止
        link = self.p.repo.get_link("L1")
        link.alternative_codes = []
        # 连续 4 分钟超时
        for i in range(4):
            self.p.telemetry.ingest("L1", t0 + i * 60, 50.0, 0.999)
        det = self.p.operations.detect_degradation("L1", t0, t0 + 600)
        self.assertEqual(len(det["created_incidents"]), 1)
        res = self.p.repo.get_reservation(code)
        self.assertEqual(res.status, ReservationStatus.ABORTED)

    def test_quality_degrades_on_telemetry_event(self) -> None:
        t0 = utc(2025, 9, 3, 9)
        code = reserve(self.p, "QA", "L1", t0, t0 + 1800, 100.0, latency="low")
        link = self.p.repo.get_link("L1")
        link.alternative_codes = []
        for i in range(4):
            self.p.telemetry.ingest("L1", t0 + i * 60, 50.0, 0.999)
        det = self.p.operations.detect_degradation("L1", t0, t0 + 600)
        res = self.p.repo.get_reservation(code)
        self.assertEqual(res.status, ReservationStatus.DEGRADED)
        self.assertEqual(res.current_latency.value, "medium")
        inc = self.p.repo.get_incident(det["created_incidents"][0])
        self.assertEqual(inc.kind, IncidentKind.TELEMETRY_DEGRADATION)

    def test_cross_midnight_maintenance(self) -> None:
        # 预约 22:00 ~ 次日 02:00 先已确认；之后才排定跨午夜维护
        r_start = utc(2025, 9, 5, 22, 0)
        r_end = utc(2025, 9, 6, 2, 0)
        code = reserve(self.p, "QA", "L1", r_start, r_end, 100.0)
        # 维护窗口 23:30 ~ 次日 00:30（割接计划在预约确认后下达）
        mw_start = utc(2025, 9, 5, 23, 30)
        mw_end = utc(2025, 9, 6, 0, 30)
        self.p.catalog.schedule_maintenance("L1", mw_start, mw_end, "跨午夜割接")

        # 23:45 激活维护：预约受影响并迁移
        incidents = self.p.operations.activate_due_maintenance(utc(2025, 9, 5, 23, 45))
        self.assertEqual(len(incidents), 1)
        res = self.p.repo.get_reservation(code)
        self.assertEqual(res.status, ReservationStatus.MIGRATED)
        action = self.p.repo.list_actions(incidents[0].code)[0]
        # 影响段必须真实跨过午夜
        self.assertLess(action.effective_from, mw_end)
        self.assertEqual(action.effective_to, mw_end)


class IncidentLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.p = Platform(utc(2025, 9, 1))
        self.p.catalog.register_link("L1", "主链路", 1000.0, "low", 0.999, ["L2"])
        self.p.catalog.register_link("L2", "备链路", 1000.0, "low", 0.999)
        self.p.catalog.register_tenant("QA", "质检车间", 5000.0)

    def test_open_failure_only_affects_current_reservations(self) -> None:
        now = utc(2025, 9, 2, 10)
        self.p.clock.set(now)
        current = reserve(self.p, "QA", "L1", now - 600, now + 3600, 100.0)
        future = reserve(self.p, "QA", "L1", now + 7200, now + 10800, 100.0)

        # 无替代路径，故障结束时间未知
        self.p.repo.get_link("L1").alternative_codes = []
        inc = self.p.operations.report_failure("L1", now)  # OPEN
        self.assertEqual(self.p.repo.get_reservation(current).status, ReservationStatus.ABORTED)
        # 未来预约此刻不臆断处置
        self.assertEqual(self.p.repo.get_reservation(future).status, ReservationStatus.CONFIRMED)

        # OPEN 事件有待补偿动作，不能结算
        from industrial_capacity.application.errors import ConflictError

        with self.assertRaises(ConflictError):
            self.p.settlement.settle_incident(inc.code)

        # 恢复关闭后才可结算（中止 10 分钟 = 60 元）
        self.p.operations.close_incident(inc.code, now + 600)
        out = self.p.settlement.settle_incident(inc.code)
        self.assertEqual(out["postings"][0]["amount"], 60.0)
        # 未来预约仍保持已确认
        self.assertEqual(self.p.repo.get_reservation(future).status, ReservationStatus.CONFIRMED)

    def test_failure_recovery_restores_link(self) -> None:
        t0 = utc(2025, 9, 2, 10)
        reserve(self.p, "QA", "L1", t0, t0 + 7200, 100.0)
        inc = self.p.operations.report_failure("L1", t0 + 600)  # 未恢复
        self.assertEqual(self.p.repo.get_link("L1").status.value, "FAILED")
        closed = self.p.operations.close_incident(inc.code, t0 + 1200)
        self.assertEqual(closed.ends_at, t0 + 1200)
        self.assertEqual(self.p.repo.get_link("L1").status.value, "ACTIVE")
        for action in self.p.repo.list_actions(inc.code):
            self.assertEqual(action.effective_to, t0 + 1200)


if __name__ == "__main__":
    unittest.main()
