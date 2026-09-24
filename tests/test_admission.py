"""原子化批量准入测试：容量、配额、维护、能力、整批回滚。"""

from __future__ import annotations

import unittest

from industrial_capacity.application.platform import Platform
from industrial_capacity.domain.values import BatchStatus, RejectCode

T0 = 1_756_000_000  # 对齐到分钟不是必须，但窗口按秒计


def base_item(**over):
    item = {
        "tenant_code": "QA",
        "link_codes": ["L1"],
        "starts_at": T0,
        "ends_at": T0 + 3600,
        "bandwidth_mbps": 100.0,
        "latency_class": "low",
        "reliability": 0.99,
        "priority": "QUALITY",
    }
    item.update(over)
    return item


class AdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.p = Platform(T0 - 10)
        self.p.catalog.register_link("L1", "主链路", 1000.0, "low", 0.999)
        self.p.catalog.register_link("L2", "备链路", 1000.0, "ultra", 0.9999)
        self.p.catalog.register_tenant("QA", "质检车间", 600.0)
        self.p.catalog.register_tenant("CTL", "控制车间", 2000.0)

    def test_batch_accepted(self) -> None:
        result = self.p.admission.submit_batch([base_item(), base_item(bandwidth_mbps=200.0)])
        self.assertEqual(result.status, BatchStatus.ACCEPTED.value)
        self.assertEqual(len(result.accepted), 2)

    def test_batch_atomic_rollback_on_one_bad_item(self) -> None:
        items = [
            base_item(tenant_code="CTL", bandwidth_mbps=600.0),
            base_item(tenant_code="CTL", bandwidth_mbps=600.0),  # 叠加 1200 超容量
        ]
        result = self.p.admission.submit_batch(items)
        self.assertEqual(result.status, BatchStatus.REJECTED.value)
        self.assertEqual(result.accepted, [])
        self.assertEqual(len(self.p.repo.list_reservations()), 0)  # 整批未落库
        second = next(r for r in result.rejected if r["index"] == 1)
        codes = {r["code"] for r in second["reasons"]}
        self.assertIn(RejectCode.INSUFFICIENT_CAPACITY, codes)

    def test_explainable_reasons_collected(self) -> None:
        items = [
            base_item(  # 同时触发能力不足与配额超限
                link_codes=["L1"],
                latency_class="ultra",
                bandwidth_mbps=900.0,
            ),
        ]
        self.p.catalog.register_tenant("SMALL", "小租户", 100.0)
        result = self.p.admission.submit_batch(
            [base_item(tenant_code="SMALL", latency_class="ultra", bandwidth_mbps=900.0)]
        )
        codes = {r["code"] for r in result.rejected[0]["reasons"]}
        self.assertIn(RejectCode.LATENCY_UNSUPPORTED, codes)
        self.assertIn(RejectCode.TENANT_QUOTA_EXCEEDED, codes)

    def test_maintenance_conflict_rejects(self) -> None:
        self.p.catalog.schedule_maintenance("L1", T0 + 1800, T0 + 5400, "凌晨检修")
        result = self.p.admission.submit_batch([base_item()])
        self.assertEqual(result.status, BatchStatus.REJECTED.value)
        codes = {r["code"] for r in result.rejected[0]["reasons"]}
        self.assertIn(RejectCode.MAINTENANCE_CONFLICT, codes)

    def test_failed_link_rejects_new_reservation(self) -> None:
        self.p.catalog.set_link_status("L1", "FAILED")
        result = self.p.admission.submit_batch([base_item()])
        codes = {r["code"] for r in result.rejected[0]["reasons"]}
        self.assertIn(RejectCode.LINK_FAILED, codes)

    def test_touching_window_does_not_consume_capacity(self) -> None:
        # 既有预约恰好在新窗口开始时结束：半开区间下容量可复用
        self.p.admission.submit_batch([base_item(starts_at=T0 - 3600, ends_at=T0, bandwidth_mbps=500.0)])
        result = self.p.admission.submit_batch([base_item(bandwidth_mbps=500.0)])
        self.assertEqual(result.status, BatchStatus.ACCEPTED.value)

    def test_unknown_tenant_and_link(self) -> None:
        result = self.p.admission.submit_batch(
            [base_item(tenant_code="NOPE", link_codes=["NOPE"])]
        )
        codes = {r["code"] for r in result.rejected[0]["reasons"]}
        self.assertIn(RejectCode.TENANT_NOT_FOUND, codes)
        self.assertIn(RejectCode.LINK_NOT_FOUND, codes)

    def test_quota_check_includes_tentative_batch_items(self) -> None:
        items = [
            base_item(tenant_code="QA", bandwidth_mbps=350.0),
            base_item(tenant_code="QA", bandwidth_mbps=350.0),
        ]
        result = self.p.admission.submit_batch(items)
        self.assertEqual(result.status, BatchStatus.REJECTED.value)
        second = next(r for r in result.rejected if r["index"] == 1)
        codes = {r["code"] for r in second["reasons"]}
        self.assertIn(RejectCode.TENANT_QUOTA_EXCEEDED, codes)

    def test_malformed_items_collect_reasons_without_exception(self) -> None:
        # 一批全是畸形项：必须优雅拒绝而不是抛异常，且整批不落库
        items = [
            {"tenant_code": "QA", "link_codes": ["L1"], "starts_at": 100, "ends_at": 50,
             "bandwidth_mbps": 10, "latency_class": "wat", "reliability": 1.5},
            {"tenant_code": "QA", "link_codes": [], "starts_at": 0, "ends_at": 100,
             "bandwidth_mbps": -1, "latency_class": "low", "reliability": 0.9},
            {"tenant_code": "QA", "link_codes": ["L1"], "starts_at": 0, "ends_at": 100,
             "bandwidth_mbps": 10, "latency_class": "low", "reliability": 0.9,
             "priority": "NOPE"},
        ]
        result = self.p.admission.submit_batch(items)
        self.assertEqual(result.status, BatchStatus.REJECTED.value)
        self.assertEqual(len(result.rejected), 3)
        self.assertEqual(self.p.repo.list_reservations(), [])
        first_codes = {r["code"] for r in result.rejected[0]["reasons"]}
        self.assertIn(RejectCode.WINDOW_INVALID, first_codes)
        self.assertIn(RejectCode.BAD_REQUEST, first_codes)
        self.assertIn(RejectCode.RELIABILITY_UNSUPPORTED, first_codes)
        second_codes = {r["code"] for r in result.rejected[1]["reasons"]}
        self.assertIn(RejectCode.EMPTY_PATH, second_codes)
        third_codes = {r["code"] for r in result.rejected[2]["reasons"]}
        self.assertIn(RejectCode.BAD_REQUEST, third_codes)


if __name__ == "__main__":
    unittest.main()
