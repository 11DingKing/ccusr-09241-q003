"""结算领域规则测试:补偿额度计算、账期切分与幂等调和。"""

import unittest
from datetime import datetime, timedelta, timezone

from industrial_capacity.domain.models import (
    AffectedReservation,
    Attribution,
    BreachWindow,
    LedgerKind,
)
from industrial_capacity.domain.settlement import (
    compute_adjustments,
    compute_credit_entries,
    reconcile_open_period,
)

T0 = datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)


def make_window(start, end, committed=1000, worst=400, attribution=Attribution.PLATFORM, affected=()):
    return BreachWindow(
        window_id=f"bw-{start.isoformat()}",
        link_id="L1",
        start=start,
        end=end,
        committed_mbps=committed,
        worst_available_mbps=worst,
        attribution=attribution,
        affected=affected,
    )


def affected(rid, bw, start, end, tenant="t1"):
    return AffectedReservation(
        reservation_id=rid,
        tenant_id=tenant,
        bandwidth_mbps=bw,
        business_priority=1,
        start=start,
        end=end,
    )


class CreditComputationTests(unittest.TestCase):
    def test_credit_proportional_to_bandwidth_minutes_and_loss(self):
        # 窗口 60 分钟,服务达成率 0.4 -> 损失率 0.6;带宽 500M -> 500*60*0.6 = 18000
        window = make_window(
            T0, T0 + timedelta(hours=1),
            committed=1000, worst=400,
            affected=(affected("r1", 500, T0 - timedelta(hours=1), T0 + timedelta(hours=2)),),
        )
        entries = compute_credit_entries([window], "2026-09")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].credit_mb_minutes, 18000)
        self.assertEqual(entries[0].kind, LedgerKind.BREACH_CREDIT)

    def test_maintenance_attribution_generates_no_credit(self):
        window = make_window(
            T0, T0 + timedelta(hours=1),
            attribution=Attribution.MAINTENANCE,
            affected=(affected("r1", 500, T0, T0 + timedelta(hours=2)),),
        )
        self.assertEqual(compute_credit_entries([window], "2026-09"), [])

    def test_credit_clipped_to_reservation_active_time(self):
        # 预约只在窗口后半小时在约 -> 500*30*0.6 = 9000
        window = make_window(
            T0, T0 + timedelta(hours=1), committed=1000, worst=400,
            affected=(affected("r1", 500, T0 + timedelta(minutes=30), T0 + timedelta(hours=2)),),
        )
        entries = compute_credit_entries([window], "2026-09")
        self.assertEqual(entries[0].credit_mb_minutes, 9000)

    def test_cross_month_window_splits_by_period(self):
        # 跨月窗口:9/30 23:30 ~ 10/1 00:30,各 30 分钟
        start = datetime(2026, 9, 30, 23, 30, tzinfo=timezone.utc)
        end = datetime(2026, 10, 1, 0, 30, tzinfo=timezone.utc)
        window = make_window(
            start, end, committed=1000, worst=0,
            affected=(affected("r1", 200, start - timedelta(hours=1), end + timedelta(hours=1)),),
        )
        sep = compute_credit_entries([window], "2026-09")
        oct_ = compute_credit_entries([window], "2026-10")
        self.assertEqual(len(sep), 1)
        self.assertEqual(len(oct_), 1)
        self.assertEqual(sep[0].credit_mb_minutes, 200 * 30)  # 损失率 1.0
        self.assertEqual(oct_[0].credit_mb_minutes, 200 * 30)
        self.assertNotEqual(sep[0].dedup_key, oct_[0].dedup_key)

    def test_deterministic_ids_make_replay_identical(self):
        window = make_window(
            T0, T0 + timedelta(hours=1),
            affected=(affected("r1", 500, T0, T0 + timedelta(hours=1)),),
        )
        first = compute_credit_entries([window], "2026-09")
        second = compute_credit_entries([window], "2026-09")
        self.assertEqual([e.entry_id for e in first], [e.entry_id for e in second])
        self.assertEqual([e.dedup_key for e in first], [e.dedup_key for e in second])


class ReconcileTests(unittest.TestCase):
    def test_replay_is_noop(self):
        window = make_window(
            T0, T0 + timedelta(hours=1),
            affected=(affected("r1", 500, T0, T0 + timedelta(hours=1)),),
        )
        desired = compute_credit_entries([window], "2026-09")
        to_upsert, to_delete = reconcile_open_period(desired, desired)
        self.assertEqual(to_upsert, [])
        self.assertEqual(to_delete, [])

    def test_stale_entries_removed_and_missing_inserted(self):
        w1 = make_window(T0, T0 + timedelta(hours=1),
                         affected=(affected("r1", 500, T0, T0 + timedelta(hours=1)),))
        w2 = make_window(T0 + timedelta(hours=2), T0 + timedelta(hours=3),
                         affected=(affected("r2", 100, T0, T0 + timedelta(hours=4)),))
        old = compute_credit_entries([w1], "2026-09")
        new = compute_credit_entries([w2], "2026-09")
        to_upsert, to_delete = reconcile_open_period(new, old)
        self.assertEqual([e.dedup_key for e in to_upsert], [new[0].dedup_key])
        self.assertEqual([e.dedup_key for e in to_delete], [old[0].dedup_key])

    def test_same_key_changed_amount_is_replaced(self):
        # 窗口身份不变但可用容量更正(400 -> 900):同键条目必须被新值替换
        w_old = make_window(T0, T0 + timedelta(hours=1), committed=1000, worst=400,
                            affected=(affected("r1", 500, T0, T0 + timedelta(hours=1)),))
        w_new = make_window(T0, T0 + timedelta(hours=1), committed=1000, worst=900,
                            affected=(affected("r1", 500, T0, T0 + timedelta(hours=1)),))
        old = compute_credit_entries([w_old], "2026-09")
        new = compute_credit_entries([w_new], "2026-09")
        self.assertEqual(old[0].dedup_key, new[0].dedup_key)
        self.assertNotEqual(old[0].credit_mb_minutes, new[0].credit_mb_minutes)
        to_upsert, to_delete = reconcile_open_period(new, old)
        self.assertEqual([e.credit_mb_minutes for e in to_upsert], [new[0].credit_mb_minutes])
        self.assertEqual(to_delete, [])


class AdjustmentTests(unittest.TestCase):
    def test_delta_posted_for_sealed_period(self):
        w_old = make_window(T0, T0 + timedelta(hours=1), committed=1000, worst=400,
                            affected=(affected("r1", 500, T0, T0 + timedelta(hours=1)),))
        sealed_entries = compute_credit_entries([w_old], "2026-09")  # 18000
        # 更正后:窗口可用容量实为 900 -> 损失率 0.1 -> 500*60*0.1 = 3000
        w_new = make_window(T0, T0 + timedelta(hours=1), committed=1000, worst=900,
                            affected=(affected("r1", 500, T0, T0 + timedelta(hours=1)),))
        desired = compute_credit_entries([w_new], "2026-09")
        adjustments = compute_adjustments(
            desired, sealed_entries,
            target_period="2026-09", current_period="2026-10", now=T0,
        )
        self.assertEqual(len(adjustments), 1)
        self.assertEqual(adjustments[0].credit_mb_minutes, 3000 - 18000)  # -15000
        self.assertEqual(adjustments[0].period, "2026-10")
        self.assertEqual(adjustments[0].target_period, "2026-09")
        self.assertEqual(adjustments[0].kind, LedgerKind.CORRECTION_ADJUSTMENT)

    def test_adjustment_replay_produces_no_new_entry(self):
        w_old = make_window(T0, T0 + timedelta(hours=1), committed=1000, worst=400,
                            affected=(affected("r1", 500, T0, T0 + timedelta(hours=1)),))
        sealed_entries = compute_credit_entries([w_old], "2026-09")
        w_new = make_window(T0, T0 + timedelta(hours=1), committed=1000, worst=900,
                            affected=(affected("r1", 500, T0, T0 + timedelta(hours=1)),))
        desired = compute_credit_entries([w_new], "2026-09")
        first = compute_adjustments(
            desired, sealed_entries,
            target_period="2026-09", current_period="2026-10", now=T0,
        )
        # 重放:recorded 已包含第一次调整 -> 差额为零
        second = compute_adjustments(
            desired, sealed_entries + first,
            target_period="2026-09", current_period="2026-10", now=T0,
        )
        self.assertEqual(second, [])


if __name__ == "__main__":
    unittest.main()
