"""结算测试：连续窗口计费、幂等过账、重放不变、更正重算、封账保护。"""

from __future__ import annotations

import unittest

from industrial_capacity.application.platform import Platform
from industrial_capacity.domain.values import (
    CompensationStatus,
    EntryType,
    IncidentStatus,
    PeriodStatus,
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


class SettlementTests(unittest.TestCase):
    def setUp(self) -> None:
        # 时钟从 8 月事件期开始，稍后推进到 9 月再封 8 月账
        self.t0 = utc(2025, 8, 25, 10)
        self.p = Platform(self.t0)
        self.p.catalog.register_link("L1", "主链路", 1000.0, "low", 0.999)
        self.p.catalog.register_tenant("QA", "质检车间", 5000.0)
        self.p.catalog.register_tenant("CTL", "控制车间", 5000.0)

    def test_abort_compensation_uses_whole_window(self) -> None:
        code = reserve(self.p, "QA", "L1", self.t0, self.t0 + 3600, 100.0)
        inc = self.p.operations.report_failure(
            "L1", self.t0 + 600, self.t0 + 1200
        )  # 无替代 → 中止
        out = self.p.settlement.settle_incident(inc.code)
        posting = out["postings"][0]
        self.assertEqual(posting["amount"], 60.0)  # 10 分钟 * 6 元
        self.assertEqual(posting["period"], "2025-08")

    def test_degraded_compensation_uses_consecutive_evidence(self) -> None:
        code = reserve(self.p, "QA", "L1", self.t0, self.t0 + 3600, 100.0)
        # 连续 5 分钟超时
        for i in range(5):
            self.p.telemetry.ingest("L1", self.t0 + i * 60, 80.0, 0.999)
        det = self.p.operations.detect_degradation("L1", self.t0, self.t0 + 600)
        inc = det["created_incidents"][0]
        out = self.p.settlement.settle_incident(inc)
        self.assertEqual(out["postings"][0]["amount"], 10.0)  # 5 分钟 * 2 元

    def test_short_jitter_no_incident_no_compensation(self) -> None:
        reserve(self.p, "QA", "L1", self.t0, self.t0 + 3600, 100.0)
        for i in range(2):  # 仅 2 分钟
            self.p.telemetry.ingest("L1", self.t0 + i * 60, 80.0, 0.999)
        det = self.p.operations.detect_degradation("L1", self.t0, self.t0 + 600)
        self.assertEqual(det["created_incidents"], [])
        self.assertEqual(self.p.settlement.settle_all()["results"], [])

    def test_settlement_is_idempotent_and_replay_keeps_ledger(self) -> None:
        reserve(self.p, "QA", "L1", self.t0, self.t0 + 3600, 100.0)
        inc = self.p.operations.report_failure("L1", self.t0 + 600, self.t0 + 1800)
        first = self.p.settlement.settle_incident(inc.code)
        self.assertFalse(first["postings"][0]["idempotent"])
        second = self.p.settlement.settle_incident(inc.code)
        self.assertTrue(second["postings"][0]["idempotent"])
        replay = self.p.settlement.replay()
        self.assertTrue(replay["replay"]["ledger_unchanged"])
        self.assertEqual(len(self.p.repo.list_ledger()), 1)

    def test_duplicate_telemetry_does_not_change_ledger(self) -> None:
        reserve(self.p, "QA", "L1", self.t0, self.t0 + 3600, 100.0)
        for i in range(4):
            self.p.telemetry.ingest("L1", self.t0 + i * 60, 80.0, 0.999)
        det = self.p.operations.detect_degradation("L1", self.t0, self.t0 + 600)
        self.p.settlement.settle_incident(det["created_incidents"][0])
        balance_before = self.p.settlement.ledger_report()["total_balance"]
        # 重复遥测：同样内容再发一遍
        for i in range(4):
            self.p.telemetry.ingest("L1", self.t0 + i * 60, 80.0, 0.999)
        self.p.settlement.replay()
        report = self.p.settlement.ledger_report()
        self.assertEqual(report["total_balance"], balance_before)
        self.assertEqual(len(report["entries"]), 1)

    def test_correction_after_period_close_uses_adjustment_in_open_period(self) -> None:
        code = reserve(self.p, "QA", "L1", self.t0, self.t0 + 3600, 100.0)
        for i in range(5):
            self.p.telemetry.ingest("L1", self.t0 + i * 60, 80.0, 0.999)
        det = self.p.operations.detect_degradation("L1", self.t0, self.t0 + 600)
        inc_code = det["created_incidents"][0]
        self.p.settlement.settle_incident(inc_code)
        # 原账：8 月 +10 元
        aug = self.p.settlement.ledger_report(period="2025-08")
        self.assertEqual(aug["balance_by_period"], {"2025-08": 10.0})

        # 封账 8 月，时钟进入 9 月
        self.p.catalog.close_period("2025-08")
        self.p.clock.set(utc(2025, 9, 10, 9))

        # 更正：前 5 分钟实际只有 2 分钟违约（其余改为正常值）
        self.p.telemetry.correct("L1", self.t0 + 0 * 60, 10.0, 0.999)
        self.p.telemetry.correct("L1", self.t0 + 1 * 60, 10.0, 0.999)
        self.p.telemetry.correct("L1", self.t0 + 2 * 60, 10.0, 0.999)
        # 只剩第 3、4 分钟违约，连续 2 分钟 < 阈值 → 事件撤销
        out = self.p.settlement.recompute_after_correction(
            "L1", self.t0, self.t0 + 600
        )
        incident = self.p.repo.get_incident(inc_code)
        self.assertIs(incident.status, IncidentStatus.REVOKED)

        report = self.p.settlement.ledger_report()
        aug_entries = [e for e in report["entries"] if e["period"] == "2025-08"]
        sep_entries = [e for e in report["entries"] if e["period"] == "2025-09"]
        # 已封账 8 月：原分录原样保留
        self.assertEqual(len(aug_entries), 1)
        self.assertEqual(aug_entries[0]["amount"], 10.0)
        # 9 月开放账期：全额冲销 -10
        self.assertEqual(len(sep_entries), 1)
        self.assertEqual(sep_entries[0]["type"], EntryType.REVERSAL.value)
        self.assertEqual(sep_entries[0]["amount"], -10.0)
        self.assertEqual(report["total_balance"], 0.0)

    def test_correction_partial_change_posts_delta(self) -> None:
        reserve(self.p, "QA", "L1", self.t0, self.t0 + 3600, 100.0)
        for i in range(5):
            self.p.telemetry.ingest("L1", self.t0 + i * 60, 80.0, 0.999)
        det = self.p.operations.detect_degradation("L1", self.t0, self.t0 + 600)
        inc_code = det["created_incidents"][0]
        self.p.settlement.settle_incident(inc_code)  # 5 分钟 = 10 元

        self.p.catalog.close_period("2025-08")
        self.p.clock.set(utc(2025, 9, 10, 9))
        # 更正：仅修正第 0 分钟，仍剩连续 4 分钟违约（1~4）
        self.p.telemetry.correct("L1", self.t0, 10.0, 0.999)
        self.p.settlement.recompute_after_correction("L1", self.t0, self.t0 + 600)

        report = self.p.settlement.ledger_report()
        aug = [e for e in report["entries"] if e["period"] == "2025-08"]
        sep = [e for e in report["entries"] if e["period"] == "2025-09"]
        self.assertEqual(aug[0]["amount"], 10.0)  # 封账不动
        self.assertEqual(sep[0]["type"], EntryType.ADJUSTMENT.value)
        self.assertEqual(sep[0]["amount"], -2.0)  # 4 分钟 = 8 元，差额 -2
        self.assertEqual(report["total_balance"], 8.0)

        # 补偿历史保留两版
        latest = self.p.repo.list_compensations(inc_code)[0]
        self.assertEqual(latest.amount, 8.0)
        self.assertEqual(latest.version, 2)
        self.assertEqual(latest.status, CompensationStatus.POSTED)
        history = self.p.repo.list_compensation_versions(latest.idempotency_key)
        self.assertEqual([c.status for c in history], [
            CompensationStatus.SUPERSEDED, CompensationStatus.POSTED
        ])

    def test_cannot_post_into_closed_period_directly(self) -> None:
        reserve(self.p, "QA", "L1", self.t0, self.t0 + 3600, 100.0)
        self.p.catalog.close_period("2025-08")
        # 8 月新发生的补偿性事件不能再写入 8 月
        inc = self.p.operations.report_failure("L1", self.t0 + 600, self.t0 + 900)
        from industrial_capacity.application.errors import PeriodClosedError

        with self.assertRaises(PeriodClosedError):
            self.p.settlement.settle_incident(inc.code)

    def test_telemetry_versions_are_auditable(self) -> None:
        self.p.telemetry.ingest("L1", self.t0, 80.0, 0.999)
        self.p.telemetry.ingest("L1", self.t0, 90.0, 0.998)  # 更正
        versions = self.p.telemetry.list_versions("L1", self.t0)
        self.assertEqual([v.version for v in versions], [1, 2])
        self.assertTrue(versions[0].superseded)
        self.assertFalse(versions[1].superseded)


if __name__ == "__main__":
    unittest.main()
