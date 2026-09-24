"""本地 API 场景测试：真实 HTTP 驱动线程化服务器。

覆盖需求指定的四类场景：
  1. 并发预约：多线程同时提交，容量不超卖、批量原子化；
  2. 跨午夜窗口：维护与预约横跨日期边界，处置区间正确；
  3. 部分链路故障：按业务优先级迁移/中止，恢复后结算；
  4. 结算重放：重复结算/重复遥测账本不变，更正重算不污染封账月份。
"""

from __future__ import annotations

import threading
import unittest
from datetime import datetime, timezone

from industrial_capacity.application.platform import Platform
from industrial_capacity.interfaces.api import create_server

from _support import ApiClient, utc


class ApiScenarioTestCase(unittest.TestCase):
    """每个场景独立平台 + 真实 HTTP 服务器（随机本地端口）。"""

    start_time: int = utc(2025, 8, 25, 10)

    def setUp(self) -> None:
        self.platform = Platform(self.start_time)
        self.server = create_server("127.0.0.1", 0, self.platform)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.api = ApiClient(self.server)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    # 便捷封装
    def post(self, path: str, body: dict):
        return self.api.request("POST", path, body)

    def get(self, path: str):
        return self.api.request("GET", path)

    def setup_topology(self) -> None:
        self.post("/admin/links", {
            "code": "L1", "name": "主链路", "total_capacity_mbps": 1000,
            "supported_latency": "low", "reliability_target": 0.999,
            "alternative_codes": ["L2", "L3"],
        })
        self.post("/admin/links", {
            "code": "L2", "name": "备链路A", "total_capacity_mbps": 400,
            "supported_latency": "low", "reliability_target": 0.999,
        })
        self.post("/admin/links", {
            "code": "L3", "name": "备链路B", "total_capacity_mbps": 1000,
            "supported_latency": "medium", "reliability_target": 0.99,
        })
        self.post("/admin/tenants", {"code": "QA", "name": "质检车间", "quota_mbps": 5000})
        self.post("/admin/tenants", {"code": "CTL", "name": "控制车间", "quota_mbps": 5000})
        self.post("/admin/tenants", {"code": "DEV", "name": "协同车间", "quota_mbps": 5000})

    def reserve_item(self, tenant: str, start: int, end: int, bw: float, priority: str) -> dict:
        return {
            "tenant_code": tenant,
            "link_codes": ["L1"],
            "starts_at": start,
            "ends_at": end,
            "bandwidth_mbps": bw,
            "latency_class": "low",
            "reliability": 0.99,
            "priority": priority,
        }


class Scenario1ConcurrentAdmissionTests(ApiScenarioTestCase):
    """场景一：并发预约不挤占既有生产线。"""

    def test_concurrent_batches_never_oversell_capacity(self) -> None:
        self.setup_topology()
        t0 = self.start_time + 3600

        # 既有生产线：已确认 400Mbps，绝不能被新预约挤掉
        status, existing = self.post("/reservations/batches", {
            "items": [self.reserve_item("QA", t0, t0 + 3600, 400.0, "QUALITY")]
        })
        self.assertEqual(status, 201)
        existing_rsv = existing["accepted"][0]

        # 10 个车间并发抢剩余 600Mbps，每个要 200Mbps → 恰好 3 个成功
        barrier = threading.Barrier(10)
        results: list[tuple[int, dict]] = []
        results_lock = threading.Lock()

        def worker(idx: int) -> None:
            barrier.wait()
            st, body = self.post("/reservations/batches", {
                "items": [self.reserve_item("DEV", t0, t0 + 3600, 200.0, "COLLABORATION")]
            })
            with results_lock:
                results.append((st, body))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        accepted = [b for st, b in results if st == 201]
        rejected = [b for st, b in results if st == 409]
        self.assertEqual(len(accepted), 3)
        self.assertEqual(len(rejected), 7)

        # 每条拒绝都给出可解释原因
        for batch in rejected:
            codes = {r["code"] for r in batch["rejected"][0]["reasons"]}
            self.assertIn("INSUFFICIENT_CAPACITY", codes)

        # 既有生产线完好
        status, reservations = self.get("/reservations")
        confirmed = [r for r in reservations if r["status"] == "CONFIRMED"]
        self.assertEqual(len(confirmed), 4)  # 1 既有 + 3 新
        existing_now = next(r for r in confirmed if r["code"] == existing_rsv)
        self.assertEqual(existing_now["status"], "CONFIRMED")
        self.assertEqual(existing_now["current_links"], ["L1"])

        # 链路上任一时点占用绝不超过容量（1000）
        total = sum(r["bandwidth_mbps"] for r in confirmed)
        self.assertEqual(total, 1000.0)

    def test_concurrent_batch_atomicity(self) -> None:
        self.setup_topology()
        t0 = self.start_time + 7200
        # 两个并发的大批量，每批 3*400=1200 超容量；
        # 但两批部分接受的总和也不得超卖——整批原子保证要么全进要么全不进
        barrier = threading.Barrier(2)
        outcomes = []
        lock = threading.Lock()

        def worker(tenant: str) -> None:
            barrier.wait()
            st, body = self.post("/reservations/batches", {
                "items": [
                    self.reserve_item(tenant, t0, t0 + 600, 400.0, "QUALITY"),
                    self.reserve_item(tenant, t0, t0 + 600, 400.0, "QUALITY"),
                    self.reserve_item(tenant, t0, t0 + 600, 400.0, "QUALITY"),
                ]
            })
            with lock:
                outcomes.append((st, body))

        threads = [
            threading.Thread(target=worker, args=("QA",)),
            threading.Thread(target=worker, args=("DEV",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 每批都需要 1200 > 1000，故两批都必须整体拒绝
        for st, body in outcomes:
            self.assertEqual(st, 409)
            self.assertEqual(body["accepted"], [])
        status, reservations = self.get("/reservations")
        self.assertEqual(reservations, [])


class Scenario2CrossMidnightTests(ApiScenarioTestCase):
    """场景二：跨午夜维护窗口。"""

    def test_maintenance_across_midnight(self) -> None:
        self.setup_topology()
        r_start = utc(2025, 8, 28, 22, 0)
        r_end = utc(2025, 8, 29, 2, 0)

        # 先确认一条跨越午夜的既有预约
        status, batch = self.post("/reservations/batches", {
            "items": [self.reserve_item("QA", r_start, r_end, 100.0, "QUALITY")]
        })
        self.assertEqual(status, 201)
        rsv = batch["accepted"][0]

        # 随后下达 23:30~00:30 的跨午夜割接计划
        mw_start = utc(2025, 8, 28, 23, 30)
        mw_end = utc(2025, 8, 29, 0, 30)
        status, mw = self.post("/admin/maintenance", {
            "link_code": "L1", "starts_at": mw_start, "ends_at": mw_end,
            "note": "跨午夜核心割接",
        })
        self.assertEqual(status, 201)

        # 割接开始前，新预约若与该窗口冲突会被准入拒绝（原因可解释）
        status, reject = self.post("/reservations/batches", {
            "items": [self.reserve_item("DEV", utc(2025, 8, 28, 23, 0), utc(2025, 8, 29, 1, 0), 50.0, "COLLABORATION")]
        })
        self.assertEqual(status, 409)
        self.assertEqual(reject["rejected"][0]["reasons"][0]["code"], "MAINTENANCE_CONFLICT")

        # 23:45 激活到期维护：既有预约迁到 L2，影响区间跨过午夜
        status, incidents = self.post("/ops/maintenance/activate", {"at": utc(2025, 8, 28, 23, 45)})
        self.assertEqual(status, 201)
        self.assertEqual(len(incidents), 1)
        inc = incidents[0]
        self.assertEqual(inc["starts_at"], mw_start)
        self.assertEqual(inc["ends_at"], mw_end)

        status, actions = self.get(f"/ops/actions?incident={inc['code']}")
        action = actions[0]
        self.assertEqual(action["action"], "MIGRATED")
        self.assertEqual(action["impact"], "NONE")
        self.assertLessEqual(action["effective_from"], mw_start)
        self.assertEqual(action["effective_to"], mw_end)  # 收口于午夜后 00:30

        status, reservation = self.get(f"/reservations")
        moved = next(r for r in reservation if r["code"] == rsv)
        self.assertEqual(moved["status"], "MIGRATED")
        self.assertEqual(moved["current_links"], ["L2"])

        # 成功迁移：无补偿
        status, settled = self.post(f"/settlement/incidents/{inc['code']}/settle", {})
        self.assertEqual(status, 200)
        self.assertEqual(settled["postings"], [])


class Scenario3PartialLinkFailureTests(ApiScenarioTestCase):
    """场景三：部分链路故障下按优先级处置。"""

    def test_failure_priority_migration_and_abort(self) -> None:
        self.setup_topology()
        t0 = utc(2025, 8, 26, 10)
        end = t0 + 3600

        # 三条预约：控制 300、质检 300、协同 300；L2 仅剩 400 容量
        rsv_codes: dict[str, str] = {}
        for tenant, priority in [("CTL", "CONTROL"), ("QA", "QUALITY"), ("DEV", "COLLABORATION")]:
            st, batch = self.post("/reservations/batches", {
                "items": [self.reserve_item(tenant, t0, end, 300.0, priority)]
            })
            self.assertEqual(st, 201)
            rsv_codes[tenant] = batch["accepted"][0]

        # L1 在 10:10~10:40 部分故障（L2、L3 正常）
        st, incident = self.post("/ops/failures", {
            "link_code": "L1", "starts_at": t0 + 600, "ends_at": t0 + 2400,
            "note": "施工挖断光缆",
        })
        self.assertEqual(st, 201)

        st, actions = self.get(f"/ops/actions?incident={incident['code']}")
        by_rsv = {a["reservation_code"]: a for a in actions}

        # 控制类最高优先：迁入 L2（满足 low SLO）
        ctl_action = by_rsv[rsv_codes["CTL"]]
        self.assertEqual(ctl_action["action"], "MIGRATED")
        self.assertEqual(ctl_action["to_links"], ["L2"])

        # 质检类次优先：L2 只剩 100 容量不足 300；L3 仅支持 medium 不满足 low
        # 硬故障无法降级运行 → 中止
        qa_action = by_rsv[rsv_codes["QA"]]
        self.assertEqual(qa_action["action"], "ABORTED")

        # 协同类同理中止
        dev_action = by_rsv[rsv_codes["DEV"]]
        self.assertEqual(dev_action["action"], "ABORTED")

        # 预约最终状态
        st, reservations = self.get("/reservations")
        status_by_code = {r["code"]: r["status"] for r in reservations}
        self.assertEqual(status_by_code[rsv_codes["CTL"]], "MIGRATED")
        self.assertEqual(status_by_code[rsv_codes["QA"]], "ABORTED")
        self.assertEqual(status_by_code[rsv_codes["DEV"]], "ABORTED")

        # 故障期间新预约不能进入 L1
        st, reject = self.post("/reservations/batches", {
            "items": [self.reserve_item("QA", t0 + 700, t0 + 900, 10.0, "QUALITY")]
        })
        self.assertEqual(st, 409)
        self.assertIn("LINK_FAILED", {r["code"] for r in reject["rejected"][0]["reasons"]})

        # 恢复：事件关闭、L1 重新 ACTIVE
        st, closed = self.post(f"/ops/incidents/{incident['code']}/close", {
            "ends_at": t0 + 2400
        })
        self.assertEqual(st, 200)
        st, link = self.get("/admin/links/L1")
        self.assertEqual(link["status"], "ACTIVE")

        # 结算：迁移的控制类无补偿；两条中止按不可用 30 分钟 * 6 元 = 180
        st, settled = self.post(f"/settlement/incidents/{incident['code']}/settle", {})
        self.assertEqual(st, 200)
        amounts = sorted(p["amount"] for p in settled["postings"] if not p.get("idempotent"))
        self.assertEqual(amounts, [180.0, 180.0])
        for posting in settled["postings"]:
            if not posting.get("idempotent"):
                self.assertEqual(posting["period"], "2025-08")


class Scenario4SettlementReplayTests(ApiScenarioTestCase):
    """场景四：结算重放与遥测更正。"""

    def _seed_degraded_reservation(self) -> tuple[str, int]:
        self.setup_topology()
        t0 = self.start_time
        st, batch = self.post("/reservations/batches", {
            "items": [self.reserve_item("QA", t0, t0 + 3600, 100.0, "QUALITY")]
        })
        self.assertEqual(st, 201)
        rsv = batch["accepted"][0]
        # 占满替代链路 L2（容量 400），使退化时无法迁移；L3 仅支持 medium 不满足 low
        st, _ = self.post("/reservations/batches", {
            "items": [{
                "tenant_code": "DEV",
                "link_codes": ["L2"],
                "starts_at": t0,
                "ends_at": t0 + 3600,
                "bandwidth_mbps": 400.0,
                "latency_class": "low",
                "reliability": 0.99,
                "priority": "COLLABORATION",
            }]
        })
        self.assertEqual(st, 201)
        # L1 连续 6 分钟时延超标（low SLO 为 20ms，观测 40ms）
        for i in range(6):
            st, _ = self.post("/telemetry/L1", {
                "bucket_ts": t0 + i * 60,
                "observed_latency_ms": 40.0,
                "observed_reliability": 0.999,
            })
            self.assertEqual(st, 201)
        st, det = self.post("/ops/degradation/detect", {
            "link_code": "L1", "window_start": t0, "window_end": t0 + 600,
        })
        self.assertEqual(st, 201)
        self.assertEqual(len(det["created_incidents"]), 1)
        return det["created_incidents"][0], t0

    def test_replay_and_duplicate_telemetry_keep_ledger(self) -> None:
        inc, _t0 = self._seed_degraded_reservation()

        st, settled = self.post(f"/settlement/incidents/{inc}/settle", {})
        self.assertEqual(st, 200)
        self.assertEqual(settled["postings"][0]["amount"], 12.0)  # 6 分钟 * 2 元

        st, ledger1 = self.get("/ledger")
        self.assertEqual(len(ledger1["entries"]), 1)
        self.assertEqual(ledger1["total_balance"], 12.0)

        # 结算重放：账本不变
        st, replay = self.post("/settlement/replay", {})
        self.assertEqual(st, 200)
        self.assertTrue(replay["replay"]["ledger_unchanged"])
        self.assertEqual(replay["replay"]["entries_before"], 1)
        self.assertEqual(replay["replay"]["entries_after"], 1)

        # 重复遥测（同样内容再发一遍）后再次重放：账本仍然不变
        t0 = self.start_time
        for i in range(6):
            self.post("/telemetry/L1", {
                "bucket_ts": t0 + i * 60,
                "observed_latency_ms": 40.0,
                "observed_reliability": 0.999,
            })
        st, replay2 = self.post("/settlement/replay", {})
        self.assertTrue(replay2["replay"]["ledger_unchanged"])
        st, ledger2 = self.get("/ledger")
        self.assertEqual(len(ledger2["entries"]), 1)
        self.assertEqual(ledger2["total_balance"], 12.0)

    def test_correction_after_close_recomputes_without_polluting_closed_month(self) -> None:
        inc, t0 = self._seed_degraded_reservation()
        self.post(f"/settlement/incidents/{inc}/settle", {})

        # 封账 8 月，时钟推进到 9 月
        st, _ = self.post("/admin/periods/close", {"period": "2025-08"})
        self.assertEqual(st, 200)
        self.post("/admin/clock/set", {"ts": utc(2025, 9, 12, 9)})

        # 更正历史遥测：6 分钟中前 4 分钟实际正常，只剩 2 分钟连续 → 违约不成立
        for i in range(4):
            st, corrected = self.post(f"/telemetry/L1/correct", {
                "bucket_ts": t0 + i * 60,
                "observed_latency_ms": 8.0,
                "observed_reliability": 0.9995,
            })
            self.assertEqual(st, 200)
            self.assertEqual(corrected["version"], 2)  # 旧版本保留、版本递增

        st, recomputed = self.post("/settlement/recompute", {
            "link_code": "L1", "window_start": t0, "window_end": t0 + 600,
        })
        self.assertEqual(st, 200)

        # 事件已被撤销
        st, incidents = self.get("/ops/incidents")
        target = next(i for i in incidents if i["code"] == inc)
        self.assertEqual(target["status"], "REVOKED")

        # 账本：8 月原封不动；9 月追加一笔全额冲销
        st, ledger = self.get("/ledger")
        aug = [e for e in ledger["entries"] if e["period"] == "2025-08"]
        sep = [e for e in ledger["entries"] if e["period"] == "2025-09"]
        self.assertEqual(len(aug), 1)
        self.assertEqual(aug[0]["amount"], 12.0)
        self.assertEqual(aug[0]["period_status"], "CLOSED")
        self.assertEqual(len(sep), 1)
        self.assertEqual(sep[0]["type"], "REVERSAL")
        self.assertEqual(sep[0]["amount"], -12.0)
        self.assertEqual(sep[0]["period_status"], "OPEN")
        self.assertEqual(ledger["total_balance"], 0.0)

        # 再次重放：冲销幂等，不会产生第二笔
        st, replay = self.post("/settlement/replay", {})
        self.assertTrue(replay["replay"]["ledger_unchanged"])
        st, ledger2 = self.get("/ledger")
        self.assertEqual(len(ledger2["entries"]), 2)

        # 历史遥测两个版本均可审计
        st, versions = self.get(f"/telemetry/L1/versions?bucket_ts={t0}")
        self.assertEqual([v["version"] for v in versions], [1, 2])
        self.assertTrue(versions[0]["superseded"])
        self.assertFalse(versions[1]["superseded"])


if __name__ == "__main__":
    unittest.main()
