"""本地 API 场景测试:并发预约、跨午夜窗口、部分链路故障、结算重放与历史更正。

每个场景通过真实 HTTP 接口驱动(线程化服务器 + 随机端口),
时钟与 ID 生成器可替换,保证结果可稳定复现。
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from industrial_capacity.adapters.clock import MutableClock
from industrial_capacity.adapters.idgen import SequentialGenerator
from industrial_capacity.app import create_app
from industrial_capacity.interfaces.http_api import make_server

T0 = datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)


class ApiClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        return self.request("POST", path, body)

    def put(self, path: str, body: dict) -> tuple[int, dict]:
        return self.request("PUT", path, body)

    def get(self, path: str) -> tuple[int, dict]:
        return self.request("GET", path)


class ApiScenarioBase(unittest.TestCase):
    """每个用例一套全新应用与服务器,时钟固定在 T0。"""

    def setUp(self) -> None:
        self.clock = MutableClock(T0)
        self.app = create_app(clock=self.clock, id_gen=SequentialGenerator())
        self.server = make_server(self.app, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.api = ApiClient(f"http://127.0.0.1:{self.port}")

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    # ---- 场景搭建助手 ----
    def create_link(self, link_id: str, capacity: int, latency: str = "ultra") -> dict:
        status, body = self.api.post(
            "/links",
            {
                "link_id": link_id,
                "name": f"链路{link_id}",
                "capacity_mbps": capacity,
                "latency_class": latency,
                "reliability_target": 0.999,
                "workshops": ["总装车间"],
            },
        )
        self.assertEqual(status, 201, body)
        return body

    def set_quota(self, tenant: str, mbps: int = 100000, count: int = 100) -> None:
        status, body = self.api.put(
            f"/tenants/{tenant}/quota",
            {"max_mbps": mbps, "max_active_reservations": count},
        )
        self.assertEqual(status, 200, body)

    def submit_batch(self, tenant: str, items: list[dict], batch_id: str) -> tuple[int, dict]:
        return self.api.post(
            "/reservation-batches",
            {"batch_id": batch_id, "tenant_id": tenant, "items": items},
        )

    def book_one(
        self,
        tenant: str,
        batch_id: str,
        link_id: str,
        start: datetime,
        end: datetime,
        bw: int,
        prio: int = 1,
        latency: str = "low",
    ) -> dict:
        status, body = self.submit_batch(
            tenant,
            [
                {
                    "item_id": f"{batch_id}-i1",
                    "link_id": link_id,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "bandwidth_mbps": bw,
                    "latency_class": latency,
                    "business_priority": prio,
                }
            ],
            batch_id,
        )
        self.assertEqual(status, 201, body)
        self.assertEqual(body["status"], "CONFIRMED", body)
        return body

    def ingest(self, samples: list[dict]) -> tuple[int, dict]:
        return self.api.post("/telemetry/samples", {"samples": samples})

    def sample(self, sid: str, link_id: str, ts: datetime, available: int) -> dict:
        return {
            "sample_id": sid,
            "link_id": link_id,
            "ts": ts.isoformat(),
            "available_mbps": available,
            "latency_ms": 2.0,
            "loss_ratio": 0.0,
        }


class AtomicBatchScenario(ApiScenarioBase):
    """批量原子准入:任一请求失败则整批拒绝,且不留任何预约。"""

    def test_batch_is_all_or_nothing_with_explainable_reasons(self) -> None:
        self.create_link("L1", 1000)
        self.set_quota("t1")
        start = T0 + timedelta(hours=1)
        end = T0 + timedelta(hours=2)
        status, body = self.submit_batch(
            "t1",
            [
                {
                    "item_id": "ok-1",
                    "link_id": "L1",
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "bandwidth_mbps": 200,
                    "latency_class": "low",
                    "business_priority": 1,
                },
                {
                    "item_id": "bad-1",
                    "link_id": "L-不存在",
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "bandwidth_mbps": 100,
                    "latency_class": "low",
                    "business_priority": 1,
                },
                {
                    "item_id": "bad-2",
                    "link_id": "L1",
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "bandwidth_mbps": 900,
                    "latency_class": "ultra",
                    "business_priority": 1,
                },
            ],
            "batch-atomic",
        )
        self.assertEqual(body["status"], "REJECTED")
        reasons = {r["item_id"]: r["code"] for r in body["rejections"]}
        # bad-1 链路不存在;bad-2 与 ok-1 合并后超容;ok-1 本身合法但随批拒绝
        self.assertEqual(reasons["bad-1"], "LINK_NOT_FOUND")
        self.assertEqual(reasons["bad-2"], "CAPACITY_EXCEEDED")
        self.assertNotIn("ok-1", reasons)
        message = next(r["message"] for r in body["rejections"] if r["item_id"] == "bad-2")
        self.assertIn("有效容量 1000", message)

        # 整批拒绝:不产生任何预约
        _, reservations = self.api.get("/reservations?tenant_id=t1")
        self.assertEqual(reservations["reservations"], [])

        # 重放同一 batch_id:返回首次判定,不会意外确认
        status, replay = self.api.get("/reservation-batches/batch-atomic")
        self.assertEqual(replay["status"], "REJECTED")
        self.assertEqual(len(replay["rejections"]), 2)


class ConcurrentAdmissionScenario(ApiScenarioBase):
    """并发预约:同一链路容量竞争下,批量准入判定必须原子且不重不超。"""

    def test_concurrent_batches_never_overcommit(self) -> None:
        self.create_link("L1", 1000)
        self.set_quota("t1")
        start = T0 + timedelta(hours=1)
        end = T0 + timedelta(hours=3)

        results: list[tuple[int, dict]] = []
        lock = threading.Lock()

        def submit(i: int) -> None:
            result = self.submit_batch(
                "t1",
                [
                    {
                        "item_id": f"b{i}-i1",
                        "link_id": "L1",
                        "start": start.isoformat(),
                        "end": end.isoformat(),
                        "bandwidth_mbps": 400,
                        "latency_class": "low",
                        "business_priority": 1,
                    }
                ],
                f"batch-{i}",
            )
            with lock:
                results.append(result)

        threads = [threading.Thread(target=submit, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        confirmed = [b for _, b in results if b["status"] == "CONFIRMED"]
        rejected = [b for _, b in results if b["status"] == "REJECTED"]
        # 容量 1000,每单 400 -> 恰好 2 单确认,其余拒绝且原因可解释
        self.assertEqual(len(confirmed), 2, results)
        self.assertEqual(len(rejected), 6, results)
        for body in rejected:
            self.assertEqual(body["rejections"][0]["code"], "CAPACITY_EXCEEDED")
            self.assertIn("可用容量不足", body["rejections"][0]["message"])

        # 幂等重放:同一 batch_id 再次提交,返回首次判定且不重复占容
        status, replay = self.submit_batch(
            "t1",
            [
                {
                    "item_id": "b0-i1",
                    "link_id": "L1",
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "bandwidth_mbps": 400,
                    "latency_class": "low",
                    "business_priority": 1,
                }
            ],
            "batch-0",
        )
        first = next(b for _, b in results if b["batch_id"] == "batch-0")
        self.assertEqual(replay, first)

        _, reservations = self.api.get("/reservations?tenant_id=t1")
        self.assertEqual(len(reservations["reservations"]), 2)
        total = sum(r["bandwidth_mbps"] for r in reservations["reservations"])
        self.assertEqual(total, 800)


class CrossMidnightScenario(ApiScenarioBase):
    """跨午夜窗口:维护窗口与容量核算在子夜前后保持连续。"""

    def test_maintenance_and_capacity_across_midnight(self) -> None:
        self.create_link("L1", 500)
        self.create_link("L2", 500)
        self.set_quota("t1")
        day = T0.replace(hour=0, minute=0)  # 当日 00:00

        # L1 维护窗口:23:30 ~ 次日 00:30 全停
        status, mw = self.api.post(
            "/links/L1/maintenance-windows",
            {
                "window_id": "mw-night",
                "start": (day + timedelta(hours=23, minutes=30)).isoformat(),
                "end": (day + timedelta(days=1, minutes=30)).isoformat(),
                "kind": "FULL_OUTAGE",
            },
        )
        self.assertEqual(status, 201, mw)

        def book(batch_id, link, start: datetime, end: datetime, bw):
            return self.submit_batch(
                "t1",
                [
                    {
                        "item_id": f"{batch_id}-i1",
                        "link_id": link,
                        "start": start.isoformat(),
                        "end": end.isoformat(),
                        "bandwidth_mbps": bw,
                        "latency_class": "low",
                        "business_priority": 1,
                    }
                ],
                batch_id,
            )

        # 维护前:22:00~23:00 确认
        _, before = book("b-before", "L1", day + timedelta(hours=22), day + timedelta(hours=23), 200)
        self.assertEqual(before["status"], "CONFIRMED")
        # 跨维护窗口:23:00~次日01:00 拒绝,原因为维护冲突
        _, during = book(
            "b-during",
            "L1",
            day + timedelta(hours=23),
            day + timedelta(days=1, hours=1),
            200,
        )
        self.assertEqual(during["status"], "REJECTED")
        self.assertEqual(during["rejections"][0]["code"], "MAINTENANCE_CONFLICT")
        # 维护后:次日00:30~02:00 确认
        _, after = book(
            "b-after",
            "L1",
            day + timedelta(days=1, minutes=30),
            day + timedelta(days=1, hours=2),
            200,
        )
        self.assertEqual(after["status"], "CONFIRMED")

        # L2 跨午夜容量核算:22:00~次日02:00 占 400M
        _, night = book(
            "b-night", "L2", day + timedelta(hours=22), day + timedelta(days=1, hours=2), 400
        )
        self.assertEqual(night["status"], "CONFIRMED")
        # 子夜前后叠加 200M -> 超容拒绝;100M -> 恰好容纳
        _, over = book(
            "b-over", "L2", day + timedelta(hours=23), day + timedelta(days=1, hours=1), 200
        )
        self.assertEqual(over["status"], "REJECTED")
        self.assertEqual(over["rejections"][0]["code"], "CAPACITY_EXCEEDED")
        _, fit = book(
            "b-fit", "L2", day + timedelta(hours=23), day + timedelta(days=1, hours=1), 100
        )
        self.assertEqual(fit["status"], "CONFIRMED")


class PartialLinkFailureScenario(ApiScenarioBase):
    """部分链路故障:连续观测确认退化,按业务优先级迁移/降级,结算生成补偿。"""

    def test_partial_failure_remediation_and_credit(self) -> None:
        self.create_link("L1", 1000)
        self.create_link("L2", 500)
        self.set_quota("t1")
        start = T0 + timedelta(hours=1)  # 09:00
        end = T0 + timedelta(hours=4)  # 12:00

        high = self.book_one("t1", "b-high", "L1", start, end, 400, prio=10)
        mid = self.book_one("t1", "b-mid", "L1", start, end, 300, prio=5)
        low = self.book_one("t1", "b-low", "L1", start, end, 300, prio=1)
        r_high, r_mid, r_low = (
            high["reservation_ids"][0],
            mid["reservation_ids"][0],
            low["reservation_ids"][0],
        )

        # 09:00 链路可用容量跌至 500(单样本,未达连续门槛,不触发)
        self.clock.set(start)
        status, result = self.ingest([self.sample("s1", "L1", start, 500)])
        self.assertEqual(result["accepted"], 1)
        _, actions = self.api.get("/remediation-actions")
        self.assertEqual(actions["remediation_actions"], [])

        # 09:05 连续第二个违约样本 -> 确认退化,触发处置
        self.clock.set(start + timedelta(minutes=5))
        self.ingest([self.sample("s2", "L1", start + timedelta(minutes=5), 500)])
        _, actions = self.api.get("/remediation-actions")
        kinds = {a["reservation_id"]: a["kind"] for a in actions["remediation_actions"]}
        # 高优先级迁移到 L2;中优先级降级到 200M;低优先级保持(500 恰好够用)
        self.assertEqual(kinds[r_high], "MIGRATE")
        self.assertEqual(kinds[r_mid], "DEGRADE")
        self.assertNotIn(r_low, kinds)

        _, r_high_view = self.api.get(f"/reservations/{r_high}")
        self.assertEqual(r_high_view["link_id"], "L2")
        self.assertEqual(r_high_view["status"], "MIGRATED")
        _, r_mid_view = self.api.get(f"/reservations/{r_mid}")
        self.assertEqual(r_mid_view["bandwidth_mbps"], 200)
        self.assertEqual(r_mid_view["status"], "DEGRADED")

        # 重复评估同一事件不重复处置(幂等)
        self.ingest([self.sample("s2", "L1", start + timedelta(minutes=5), 500)])
        _, actions_after = self.api.get("/remediation-actions")
        self.assertEqual(len(actions_after["remediation_actions"]), 2)

        # 09:10 链路恢复到 1000 -> 违约窗口闭合 [09:00, 09:10)
        self.clock.set(start + timedelta(minutes=10))
        self.ingest([self.sample("s3", "L1", start + timedelta(minutes=10), 1000)])
        _, windows = self.api.get("/breach-windows?link_id=L1")
        self.assertEqual(len(windows["breach_windows"]), 1)
        window = windows["breach_windows"][0]
        self.assertEqual(window["attribution"], "PLATFORM")
        self.assertEqual(window["committed_mbps"], 1000)
        self.assertEqual(window["worst_available_mbps"], 500)
        self.assertEqual(len(window["affected"]), 3)

        # 结算:补偿 = 快照带宽 x 10 分钟 x (1 - 0.5)
        self.clock.set(start + timedelta(hours=4))
        status, settled = self.api.post("/settlements/2026-09/run", {})
        self.assertEqual(status, 200, settled)
        self.assertEqual(settled["created"], 3)
        _, ledger = self.api.get("/ledger?tenant_id=t1&period=2026-09")
        credits = {e["reservation_id"]: e["credit_mb_minutes"] for e in ledger["ledger"]}
        self.assertEqual(credits[r_high], 400 * 10 * 0.5)
        self.assertEqual(credits[r_mid], 300 * 10 * 0.5)
        self.assertEqual(credits[r_low], 300 * 10 * 0.5)


class SettlementReplayScenario(ApiScenarioBase):
    """结算重放与重复遥测:账本不因重复输入而改变。"""

    def _build_breach(self) -> str:
        self.create_link("L1", 1000)
        self.set_quota("t1")
        start = T0 + timedelta(hours=1)
        end = T0 + timedelta(hours=3)
        batch = self.book_one("t1", "b1", "L1", start, end, 800, prio=5)
        # 连续 3 个样本可用 400,随后恢复
        for i in range(3):
            self.clock.set(start + timedelta(minutes=5 * i))
            self.ingest([self.sample(f"s{i}", "L1", start + timedelta(minutes=5 * i), 400)])
        self.clock.set(start + timedelta(minutes=15))
        self.ingest([self.sample("s3", "L1", start + timedelta(minutes=15), 1000)])
        return batch["reservation_ids"][0]

    def test_duplicate_telemetry_and_settlement_replay_keep_ledger(self) -> None:
        reservation_id = self._build_breach()

        # 重复上报相同样本:全部被识别为重复,观测不变
        start = T0 + timedelta(hours=1)
        _, dup = self.ingest([self.sample("s0", "L1", start, 400)])
        self.assertEqual(dup["duplicates"], 1)
        self.assertEqual(dup["accepted"], 0)

        self.clock.set(start + timedelta(hours=3))
        # 第一次结算:窗口 [+1h, +1h10) 共 10 分钟(处置生效后需求被满足,窗口闭合),
        # 服务率 0.5 -> 800*10*0.5 = 4000
        status, first = self.api.post("/settlements/2026-09/run", {})
        self.assertEqual(status, 200, first)
        _, ledger1 = self.api.get("/ledger?period=2026-09")
        self.assertEqual(len(ledger1["ledger"]), 1)
        entry = ledger1["ledger"][0]
        self.assertEqual(entry["reservation_id"], reservation_id)
        self.assertEqual(entry["credit_mb_minutes"], 800 * 10 // 2)

        # 结算重放:账本逐字节一致
        status, second = self.api.post("/settlements/2026-09/run", {})
        self.assertEqual(status, 200, second)
        self.assertTrue(second["replay"])
        _, ledger2 = self.api.get("/ledger?period=2026-09")
        self.assertEqual(ledger1, ledger2)

        # 封账后禁止再结算
        status, sealed = self.api.post("/settlements/2026-09/seal", {})
        self.assertEqual(status, 200, sealed)
        self.assertEqual(sealed["status"], "SEALED")
        status, err = self.api.post("/settlements/2026-09/run", {})
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "PERIOD_SEALED")
        _, ledger3 = self.api.get("/ledger?period=2026-09")
        self.assertEqual(ledger1, ledger3)


class CrossMonthBreachScenario(ApiScenarioBase):
    """跨月(跨午夜)违约窗口:补偿按账期切分,两侧账期各自结算。"""

    def test_breach_window_spanning_month_boundary_splits_credits(self) -> None:
        # 9 月 30 日 22:00 起步
        self.clock.set(datetime(2026, 9, 30, 22, 0, tzinfo=timezone.utc))
        self.create_link("L1", 1000)
        self.create_link("L2", 1000)
        self.set_quota("t1")
        start = datetime(2026, 9, 30, 23, 0, tzinfo=timezone.utc)
        end = datetime(2026, 10, 1, 2, 0, tzinfo=timezone.utc)
        batch = self.book_one("t1", "b-x", "L1", start, end, 600, prio=5)
        reservation_id = batch["reservation_ids"][0]

        # 23:55 与 次日00:00 连续违约(可用 200),00:05 恢复
        self.clock.set(datetime(2026, 9, 30, 23, 55, tzinfo=timezone.utc))
        self.ingest([self.sample("s1", "L1", datetime(2026, 9, 30, 23, 55, tzinfo=timezone.utc), 200)])
        self.clock.set(datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc))
        self.ingest([self.sample("s2", "L1", datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc), 200)])
        self.clock.set(datetime(2026, 10, 1, 0, 5, tzinfo=timezone.utc))
        self.ingest([self.sample("s3", "L1", datetime(2026, 10, 1, 0, 5, tzinfo=timezone.utc), 1000)])

        # 窗口 [23:55, 00:05) 横跨两个月;服务率 1/3
        _, windows = self.api.get("/breach-windows?link_id=L1")
        self.assertEqual(len(windows["breach_windows"]), 1)

        self.clock.set(datetime(2026, 10, 1, 1, 0, tzinfo=timezone.utc))
        status, sep = self.api.post("/settlements/2026-09/run", {})
        self.assertEqual(status, 200, sep)
        status, oct_ = self.api.post("/settlements/2026-10/run", {})
        self.assertEqual(status, 200, oct_)

        _, ledger_sep = self.api.get("/ledger?period=2026-09")
        _, ledger_oct = self.api.get("/ledger?period=2026-10")
        self.assertEqual(len(ledger_sep["ledger"]), 1)
        self.assertEqual(len(ledger_oct["ledger"]), 1)
        # 每侧 5 分钟:600 * 5 * (1 - 200/600) = 2000
        self.assertEqual(ledger_sep["ledger"][0]["credit_mb_minutes"], 2000)
        self.assertEqual(ledger_oct["ledger"][0]["credit_mb_minutes"], 2000)
        self.assertEqual(ledger_sep["ledger"][0]["reservation_id"], reservation_id)


class MaintenanceRemediationScenario(ApiScenarioBase):
    """维护窗口冲击已确认预约:按业务优先级迁移或中止。"""

    def test_maintenance_window_triggers_priority_remediation(self) -> None:
        self.create_link("L1", 1000)
        self.create_link("L2", 400)
        self.set_quota("t1")
        start = T0 + timedelta(hours=1)
        end = T0 + timedelta(hours=4)
        high = self.book_one("t1", "b-high", "L1", start, end, 300, prio=10)
        low = self.book_one("t1", "b-low", "L1", start, end, 300, prio=1)
        r_high, r_low = high["reservation_ids"][0], low["reservation_ids"][0]

        # 计划维护:T0+2h ~ T0+3h 全停,与两条预约相交
        status, mw = self.api.post(
            "/links/L1/maintenance-windows",
            {
                "window_id": "mw-1",
                "start": (T0 + timedelta(hours=2)).isoformat(),
                "end": (T0 + timedelta(hours=3)).isoformat(),
                "kind": "FULL_OUTAGE",
            },
        )
        self.assertEqual(status, 201, mw)
        kinds = {a["reservation_id"]: a["kind"] for a in mw["remediation_actions"]}
        # 高优先级迁往 L2;L2 余量不足,低优先级中止
        self.assertEqual(kinds[r_high], "MIGRATE")
        self.assertEqual(kinds[r_low], "TERMINATE")

        _, r_high_view = self.api.get(f"/reservations/{r_high}")
        self.assertEqual(r_high_view["link_id"], "L2")
        _, r_low_view = self.api.get(f"/reservations/{r_low}")
        self.assertEqual(r_low_view["status"], "TERMINATED")
        self.assertIsNotNone(r_low_view["terminated_at"])

        # 维护窗口内不再接受新预约;维护后时段仍可预约
        _, rejected = self.submit_batch(
            "t1",
            [
                {
                    "item_id": "x1",
                    "link_id": "L1",
                    "start": (T0 + timedelta(hours=2, minutes=10)).isoformat(),
                    "end": (T0 + timedelta(hours=2, minutes=50)).isoformat(),
                    "bandwidth_mbps": 100,
                    "latency_class": "low",
                    "business_priority": 1,
                }
            ],
            "b-blocked",
        )
        self.assertEqual(rejected["rejections"][0]["code"], "MAINTENANCE_CONFLICT")


class TelemetryCorrectionScenario(ApiScenarioBase):
    """历史遥测更正:开放账期就地重算,已封账账期只以调整条目反映。"""

    def _build_august_breach(self) -> str:
        clock_start = datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc)
        self.clock.set(clock_start)
        self.create_link("L1", 1000)
        self.create_link("L2", 1000)
        self.set_quota("t1")
        end = clock_start + timedelta(hours=2)
        batch = self.book_one("t1", "b-aug", "L1", clock_start, end, 800, prio=5)
        reservation_id = batch["reservation_ids"][0]
        # 10:00/10:05 可用 400(连续违约 -> 迁移到 L2),10:10 恢复
        self.ingest([self.sample("s1", "L1", clock_start, 400)])
        self.clock.set(clock_start + timedelta(minutes=5))
        self.ingest([self.sample("s2", "L1", clock_start + timedelta(minutes=5), 400)])
        self.clock.set(clock_start + timedelta(minutes=10))
        self.ingest([self.sample("s3", "L1", clock_start + timedelta(minutes=10), 1000)])
        self.clock.set(clock_start + timedelta(hours=3))
        return reservation_id

    def test_correction_recomputes_open_period_in_place(self) -> None:
        self._build_august_breach()
        status, settled = self.api.post("/settlements/2026-08/run", {})
        self.assertEqual(settled["created"], 1)
        _, ledger = self.api.get("/ledger?period=2026-08")
        entry = ledger["ledger"][0]
        self.assertEqual(entry["credit_mb_minutes"], 4000)  # 800*10*0.5

        # 账期仍开放:更正遥测后就地重算,条目 ID 不变、金额更新
        status, correction = self.api.post(
            "/telemetry/corrections",
            {
                "corrections": [
                    self.sample("c1", "L1", datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc), 700)
                    | {"corrects_sample_id": "s1"},
                    self.sample(
                        "c2", "L1", datetime(2026, 8, 10, 10, 5, tzinfo=timezone.utc), 700
                    )
                    | {"corrects_sample_id": "s2"},
                ]
            },
        )
        self.assertEqual(status, 200, correction)
        _, ledger_after = self.api.get("/ledger?period=2026-08")
        self.assertEqual(len(ledger_after["ledger"]), 1)
        updated = ledger_after["ledger"][0]
        self.assertEqual(updated["entry_id"], entry["entry_id"])
        self.assertEqual(updated["credit_mb_minutes"], 1000)  # 800*10*(1-7/8)

    def test_correction_recomputes_open_and_adjusts_sealed_periods(self) -> None:
        # ---- 8 月:违约、结算、封账 ----
        clock_start = datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc)
        reservation_id = self._build_august_breach()
        status, settled = self.api.post("/settlements/2026-08/run", {})
        self.assertEqual(status, 200, settled)
        _, ledger_aug = self.api.get("/ledger?period=2026-08")
        self.assertEqual(len(ledger_aug["ledger"]), 1)
        # 窗口 [10:00, 10:10),服务率 0.5 -> 800*10*0.5 = 4000
        self.assertEqual(ledger_aug["ledger"][0]["credit_mb_minutes"], 4000)
        sealed_entry_id = ledger_aug["ledger"][0]["entry_id"]
        self.api.post("/settlements/2026-08/seal", {})

        # ---- 9 月:更正 8 月遥测(实际可用为 700,违约程度减轻) ----
        self.clock.set(datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc))
        status, correction = self.api.post(
            "/telemetry/corrections",
            {
                "corrections": [
                    self.sample("c1", "L1", clock_start, 700) | {"corrects_sample_id": "s1"},
                    self.sample("c2", "L1", clock_start + timedelta(minutes=5), 700)
                    | {"corrects_sample_id": "s2"},
                ]
            },
        )
        self.assertEqual(status, 200, correction)
        self.assertEqual(correction["applied"], 2)

        # 已封账的 8 月账本保持原样
        _, ledger_aug_after = self.api.get("/ledger?period=2026-08")
        self.assertEqual(len(ledger_aug_after["ledger"]), 1)
        self.assertEqual(ledger_aug_after["ledger"][0]["entry_id"], sealed_entry_id)
        self.assertEqual(ledger_aug_after["ledger"][0]["credit_mb_minutes"], 4000)

        # 差额以调整条目计入 9 月:更正后服务率 7/8 -> 800*10*(1/8) = 1000,差额 -3000
        _, ledger_sep = self.api.get("/ledger?period=2026-09")
        self.assertEqual(len(ledger_sep["ledger"]), 1)
        adjustment = ledger_sep["ledger"][0]
        self.assertEqual(adjustment["kind"], "CORRECTION_ADJUSTMENT")
        self.assertEqual(adjustment["credit_mb_minutes"], 1000 - 4000)
        self.assertEqual(adjustment["target_period"], "2026-08")
        self.assertEqual(adjustment["reservation_id"], reservation_id)

        # 更正重放:不重复产生调整条目
        status, replay = self.api.post(
            "/telemetry/corrections",
            {"corrections": [self.sample("c1", "L1", clock_start, 700) | {"corrects_sample_id": "s1"}]},
        )
        self.assertEqual(replay["duplicates"], 1)
        _, ledger_sep_after = self.api.get("/ledger?period=2026-09")
        self.assertEqual(ledger_sep, ledger_sep_after)

        # 对同一原始样本再次更正(更严重的修正:10:00 实际可用 950):
        # 后到更正取代先到更正,窗口消失 -> 再记一笔调整使累计差额归零
        status, second = self.api.post(
            "/telemetry/corrections",
            {
                "corrections": [
                    self.sample("c3", "L1", clock_start, 950) | {"corrects_sample_id": "s1"},
                ]
            },
        )
        self.assertEqual(second["applied"], 1)
        _, ledger_sep_final = self.api.get("/ledger?period=2026-09")
        self.assertEqual(len(ledger_sep_final["ledger"]), 2)
        total_adjustment = sum(e["credit_mb_minutes"] for e in ledger_sep_final["ledger"])
        self.assertEqual(total_adjustment, -4000)  # 累计调整使 8 月有效补偿归零
        _, ledger_aug_final = self.api.get("/ledger?period=2026-08")
        self.assertEqual(ledger_aug_final, ledger_aug_after)  # 封账月始终不变


if __name__ == "__main__":
    unittest.main()
