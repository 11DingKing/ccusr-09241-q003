"""内存存储:进程内数据结构与全局锁。

所有集合由应用服务在同一把可重入锁下读写,保证"判定+落库"的原子性;
台账、批次等以业务键(dedup_key / batch_id)为索引,天然支持幂等重放。
"""

from __future__ import annotations

import itertools
import threading

from ..domain.models import (
    BreachRun,
    BreachWindow,
    LedgerEntry,
    Link,
    MaintenanceWindow,
    RemediationAction,
    Reservation,
    ReservationBatch,
    SettlementPeriod,
    TelemetrySample,
    TenantQuota,
)


class InMemoryStore:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.links: dict[str, Link] = {}
        self.maintenance: dict[str, MaintenanceWindow] = {}
        self.quotas: dict[str, TenantQuota] = {}
        self.reservations: dict[str, Reservation] = {}
        self.batches: dict[str, ReservationBatch] = {}
        self.samples: dict[str, TelemetrySample] = {}
        self.sample_seq: dict[str, int] = {}  # 样本摄取序号:后到更正取代先到观测
        self._seq_counter = itertools.count(1)
        self.runs: dict[str, BreachRun] = {}  # 进行中的违约序列,按链路索引
        self.windows: dict[str, BreachWindow] = {}  # 当前有效的闭合窗口
        self.windows_history: dict[str, BreachWindow] = {}  # 被重算取代的历史窗口(审计)
        self.actions: dict[str, RemediationAction] = {}  # 按 dedup_key 索引
        self.ledger: dict[str, LedgerEntry] = {}  # 按 dedup_key 索引
        self.periods: dict[str, SettlementPeriod] = {}

    # ---- 样本 ----
    def add_sample(self, sample: TelemetrySample) -> None:
        self.samples[sample.sample_id] = sample
        self.sample_seq[sample.sample_id] = next(self._seq_counter)

    # ---- 常用查询 ----
    def occupying_reservations(self) -> list[Reservation]:
        return [r for r in self.reservations.values() if r.status.occupies_capacity]

    def samples_for_link(self, link_id: str) -> list[TelemetrySample]:
        """链路的有效样本流:更正取代原始样本;同一时刻以后到的观测为准。"""
        samples = [s for s in self.samples.values() if s.link_id == link_id]
        corrected_ids = {s.corrects_sample_id for s in samples if s.corrects_sample_id}
        active = [s for s in samples if s.sample_id not in corrected_ids]
        by_ts: dict = {}
        for sample in active:
            current = by_ts.get(sample.ts)
            if current is None or self.sample_seq[sample.sample_id] > self.sample_seq[current.sample_id]:
                by_ts[sample.ts] = sample
        return sorted(by_ts.values(), key=lambda s: s.ts)

    def ledger_entries(self, *, period: str | None = None, tenant_id: str | None = None) -> list:
        entries = list(self.ledger.values())
        if period is not None:
            entries = [e for e in entries if e.period == period]
        if tenant_id is not None:
            entries = [e for e in entries if e.tenant_id == tenant_id]
        return sorted(entries, key=lambda e: (e.period, e.entry_id))
