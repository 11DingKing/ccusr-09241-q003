"""本地 API 端到端演示：在随机本地端口启动真实 HTTP 服务，依次展示

  1. 并发预约的原子化准入（不挤占既有生产线）
  2. 跨午夜维护窗口的迁移处置
  3. 部分链路故障下按业务优先级迁移/中止与结算
  4. 结算重放幂等、遥测更正重算且不污染已封账月份

运行：
    python3 -m industrial_capacity.interfaces.demo
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

from ..application.platform import Platform
from .api import create_server

# 让 tests/_support 可直接复用
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "tests"))
from _support import ApiClient, utc  # noqa: E402

BASE = utc(2025, 8, 25, 10)


def ts(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%m-%d %H:%M")


def title(text: str) -> None:
    print("\n" + "=" * 72)
    print(text)
    print("=" * 72)


def show(label: str, value) -> None:
    print(f"\n【{label}】")
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main() -> None:
    platform = Platform(BASE)
    server = create_server("127.0.0.1", 0, platform)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    api = ApiClient(server)

    def call(method, path, body=None):
        st, data = api.request(method, path, body)
        return st, data

    # ------------------------------------------------------------------
    title("场景一：并发预约——既有生产线不被新能力预约挤占")
    call("POST", "/admin/links", {
        "code": "L1", "name": "主链路", "total_capacity_mbps": 1000,
        "supported_latency": "low", "reliability_target": 0.999,
        "alternative_codes": ["L2"],
    })
    call("POST", "/admin/links", {
        "code": "L2", "name": "备链路", "total_capacity_mbps": 400,
        "supported_latency": "low", "reliability_target": 0.999,
    })
    call("POST", "/admin/tenants", {"code": "QA", "name": "质检车间", "quota_mbps": 5000})
    call("POST", "/admin/tenants", {"code": "DEV", "name": "协同车间", "quota_mbps": 5000})

    t0 = BASE + 3600
    st, existing = call("POST", "/reservations/batches", {"items": [{
        "tenant_code": "QA", "link_codes": ["L1"], "starts_at": t0, "ends_at": t0 + 3600,
        "bandwidth_mbps": 400, "latency_class": "low", "reliability": 0.99, "priority": "QUALITY",
    }]})
    print(f"既有质检生产线已确认：{existing['accepted'][0]}（占用 400Mbps / 总容量 1000Mbps）")

    barrier = threading.Barrier(10)
    outcomes: list = []

    def worker():
        barrier.wait()
        st, body = call("POST", "/reservations/batches", {"items": [{
            "tenant_code": "DEV", "link_codes": ["L1"], "starts_at": t0, "ends_at": t0 + 3600,
            "bandwidth_mbps": 200, "latency_class": "low", "reliability": 0.99,
            "priority": "COLLABORATION",
        }]})
        outcomes.append((st, body))

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    accepted = sum(1 for st, _ in outcomes if st == 201)
    rejected = [b for st, b in outcomes if st == 409]
    print(f"10 个协同车间并发各申请 200Mbps：接受 {accepted} 批、拒绝 {len(rejected)} 批")
    print("一条被拒批量的可解释原因：")
    print(json.dumps(rejected[0]["rejected"], ensure_ascii=False, indent=2))
    st, reservations = call("GET", "/reservations")
    total = sum(r["bandwidth_mbps"] for r in reservations if r["status"] == "CONFIRMED")
    print(f"准入后链路上已确认预约总占用 = {total:g}Mbps（容量上限 1000，未超卖）")

    # ------------------------------------------------------------------
    title("场景二：跨午夜维护窗口（08-28 23:30 ~ 08-29 00:30 UTC）")
    r_start, r_end = utc(2025, 8, 28, 22), utc(2025, 8, 29, 2)
    st, batch = call("POST", "/reservations/batches", {"items": [{
        "tenant_code": "QA", "link_codes": ["L1"], "starts_at": r_start, "ends_at": r_end,
        "bandwidth_mbps": 100, "latency_class": "low", "reliability": 0.99, "priority": "QUALITY",
    }]})
    rsv = batch["accepted"][0]
    mw_start, mw_end = utc(2025, 8, 28, 23, 30), utc(2025, 8, 29, 0, 30)
    call("POST", "/admin/maintenance", {
        "link_code": "L1", "starts_at": mw_start, "ends_at": mw_end, "note": "跨午夜核心割接",
    })
    print(f"既有预约 {rsv} 窗口 {ts(r_start)}~{ts(r_end)}，维护窗口 {ts(mw_start)}~{ts(mw_end)}")

    st, reject = call("POST", "/reservations/batches", {"items": [{
        "tenant_code": "DEV", "link_codes": ["L1"],
        "starts_at": utc(2025, 8, 28, 23), "ends_at": utc(2025, 8, 29, 1),
        "bandwidth_mbps": 50, "latency_class": "low", "reliability": 0.99,
        "priority": "COLLABORATION",
    }]})
    print(f"新预约撞上维护窗口 → HTTP {st}，拒绝码：{reject['rejected'][0]['reasons'][0]['code']}")

    st, incidents = call("POST", "/ops/maintenance/activate", {"at": utc(2025, 8, 28, 23, 45)})
    inc = incidents[0]
    st, actions = call("GET", f"/ops/actions?incident={inc['code']}")
    a = actions[0]
    print(f"23:45 维护激活 → 预约处置 {a['action']}，影响 {a['impact']}，"
          f"承载路径 {a['from_links']} → {a['to_links']}")
    print(f"处置有效区间 {ts(a['effective_from'])} ~ {ts(a['effective_to'])}（真实跨过午夜）")
    st, settled = call("POST", f"/settlement/incidents/{inc['code']}/settle", {})
    print(f"结算结果：{settled['postings']}（成功迁移、无中断 → 无补偿）")

    # ------------------------------------------------------------------
    title("场景三：部分链路故障——按业务优先级迁移 / 中止")
    call("POST", "/admin/links", {
        "code": "L3", "name": "备链路B(medium)", "total_capacity_mbps": 1000,
        "supported_latency": "medium", "reliability_target": 0.99,
    })
    link = platform.repo.get_link("L1")
    link.alternative_codes = ["L2", "L3"]
    call("POST", "/admin/tenants", {"code": "CTL", "name": "控制车间", "quota_mbps": 5000})

    t1 = utc(2025, 8, 26, 10)
    codes = {}
    for tenant, priority in [("CTL", "CONTROL"), ("QA", "QUALITY"), ("DEV", "COLLABORATION")]:
        st, b = call("POST", "/reservations/batches", {"items": [{
            "tenant_code": tenant, "link_codes": ["L1"],
            "starts_at": t1, "ends_at": t1 + 3600,
            "bandwidth_mbps": 300, "latency_class": "low", "reliability": 0.99,
            "priority": priority,
        }]})
        codes[tenant] = b["accepted"][0]
    print("已确认：控制/质检/协同 各 300Mbps；备链路 L2 容量 400、L3 仅支持 medium")

    st, inc = call("POST", "/ops/failures", {
        "link_code": "L1", "starts_at": t1 + 600, "ends_at": t1 + 2400, "note": "施工挖断光缆",
    })
    st, actions = call("GET", f"/ops/actions?incident={inc['code']}")
    by = {a["reservation_code"]: a for a in actions}
    for tenant in ["CTL", "QA", "DEV"]:
        a = by[codes[tenant]]
        extra = f" → {a['to_links']}" if a["action"] == "MIGRATED" else ""
        print(f"  {tenant}（{codes[tenant]}）：{a['action']}{extra}，原因：{a['reason']}")

    call("POST", f"/ops/incidents/{inc['code']}/close", {"ends_at": t1 + 2400})
    st, settled = call("POST", f"/settlement/incidents/{inc['code']}/settle", {})
    print("故障恢复后结算（中止按 30 分钟不可用 × 6 元/分钟 = 180 元/条）：")
    for p in settled["postings"]:
        print(f"  补偿 {p.get('compensation')}: {p['amount']} 元，计入 {p.get('period')}")

    # ------------------------------------------------------------------
    title("场景四：结算重放幂等 + 遥测更正重算（不污染已封账月份）")
    t2 = utc(2025, 8, 27, 10)
    st, b = call("POST", "/reservations/batches", {"items": [{
        "tenant_code": "QA", "link_codes": ["L1"],
        "starts_at": t2, "ends_at": t2 + 3600,
        "bandwidth_mbps": 100, "latency_class": "low", "reliability": 0.99, "priority": "QUALITY",
    }]})
    # 占满 L2，使该预约只能降级
    call("POST", "/reservations/batches", {"items": [{
        "tenant_code": "DEV", "link_codes": ["L2"],
        "starts_at": t2, "ends_at": t2 + 3600,
        "bandwidth_mbps": 400, "latency_class": "low", "reliability": 0.99,
        "priority": "COLLABORATION",
    }]})
    for i in range(6):
        call("POST", "/telemetry/L1", {
            "bucket_ts": t2 + i * 60, "observed_latency_ms": 40.0, "observed_reliability": 0.999,
        })
    st, det = call("POST", "/ops/degradation/detect", {
        "link_code": "L1", "window_start": t2, "window_end": t2 + 600,
    })
    deg_inc = det["created_incidents"][0]
    st, settled = call("POST", f"/settlement/incidents/{deg_inc}/settle", {})
    amount = settled["postings"][0]["amount"]
    print(f"连续 6 分钟超时 → 降级处置，补偿 {amount} 元计入 2025-08")

    st, replay1 = call("POST", "/settlement/replay", {})
    for i in range(6):  # 重复遥测再发一遍
        call("POST", "/telemetry/L1", {
            "bucket_ts": t2 + i * 60, "observed_latency_ms": 40.0, "observed_reliability": 0.999,
        })
    st, replay2 = call("POST", "/settlement/replay", {})
    print(f"结算重放 ×2（其间重发重复遥测）：账本不变 = {replay2['replay']['ledger_unchanged']}")

    call("POST", "/admin/periods/close", {"period": "2025-08"})
    call("POST", "/admin/clock/set", {"ts": utc(2025, 9, 12, 9)})
    print("8 月已封账，时钟推进到 9 月；现更正：6 分钟中前 4 分钟实际正常")
    for i in range(4):
        call("POST", "/telemetry/L1/correct", {
            "bucket_ts": t2 + i * 60, "observed_latency_ms": 8.0, "observed_reliability": 0.9995,
        })
    st, rec = call("POST", "/settlement/recompute", {
        "link_code": "L1", "window_start": t2, "window_end": t2 + 600,
    })
    print(f"受影响时段重算：新事件 {rec['new_incidents']}，重算事件 {rec['revoked_or_kept']}")

    st, ledger = call("GET", "/ledger")
    print("最终账本：")
    for e in ledger["entries"]:
        print(f"  [{e['period_status']}] {e['period']}  {e['type']:<12} "
              f"{e['amount']:>7.2f} 元  {e['note']}")
    print(f"账期余额：{ledger['balance_by_period']}；合计 {ledger['total_balance']} 元")
    print("→ 8 月封账分录原样保留，冲销全额计入 9 月开放账期；再次重放账本仍不变。")

    server.shutdown()
    server.server_close()


if __name__ == "__main__":
    main()
