"""内存持久化适配器：单一互斥锁提供事务原子性。

适合本地单进程 API 与场景测试；Repository 协议允许后续替换为数据库实现，
而用例层与接口层无需改动。所有集合访问都应在 transaction() 临界区内进行。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

from ..application.errors import ConflictError
from ..domain import entities as e
from ..domain.values import IncidentStatus


class InMemoryRepository:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._links: dict[str, e.Link] = {}
        self._tenants: dict[str, e.Tenant] = {}
        self._maintenance: list[e.MaintenanceWindow] = []
        self._reservations: dict[str, e.Reservation] = {}
        self._batches: dict[str, e.BatchResult] = {}
        # (link_code, bucket_ts) -> 按版本排列的全部遥测版本
        self._telemetry: dict[tuple[str, int], list[e.TelemetrySample]] = {}
        self._incidents: dict[str, e.Incident] = {}
        self._actions: list[e.ReservationAction] = []
        # 幂等键 -> 按版本排列的补偿历史（旧版本 SUPERSEDED 保留可审计）
        self._compensations: dict[str, list[e.Compensation]] = {}
        self._ledger: list[e.LedgerEntry] = []
        self._periods: dict[str, e.AccountingPeriod] = {}

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self._lock.acquire()
        try:
            yield
        finally:
            self._lock.release()

    # ----- 链路 -----
    def save_link(self, link: e.Link) -> None:
        self._links[link.code] = link

    def get_link(self, code: str) -> e.Link | None:
        return self._links.get(code)

    def list_links(self) -> list[e.Link]:
        return sorted(self._links.values(), key=lambda x: x.code)

    # ----- 租户 -----
    def save_tenant(self, tenant: e.Tenant) -> None:
        self._tenants[tenant.code] = tenant

    def get_tenant(self, code: str) -> e.Tenant | None:
        return self._tenants.get(code)

    def list_tenants(self) -> list[e.Tenant]:
        return sorted(self._tenants.values(), key=lambda x: x.code)

    # ----- 维护窗口 -----
    def add_maintenance(self, window: e.MaintenanceWindow) -> None:
        self._maintenance.append(window)

    def list_maintenance(self, link_code: str | None = None) -> list[e.MaintenanceWindow]:
        rows = self._maintenance
        if link_code is not None:
            rows = [w for w in rows if w.link_code == link_code]
        return sorted(rows, key=lambda w: (w.link_code, w.starts_at, w.code))

    # ----- 预约 -----
    def add_reservation(self, reservation: e.Reservation) -> None:
        if reservation.code in self._reservations:
            raise ConflictError(f"预约 {reservation.code} 已存在")
        self._reservations[reservation.code] = reservation

    def get_reservation(self, code: str) -> e.Reservation | None:
        return self._reservations.get(code)

    def list_reservations(self, tenant_code: str | None = None) -> list[e.Reservation]:
        rows = list(self._reservations.values())
        if tenant_code is not None:
            rows = [r for r in rows if r.tenant_code == tenant_code]
        return sorted(rows, key=lambda r: (r.starts_at, r.code))

    def update_reservation(self, reservation: e.Reservation) -> None:
        self._reservations[reservation.code] = reservation

    # ----- 批量结果 -----
    def save_batch(self, result: e.BatchResult) -> None:
        self._batches[result.code] = result

    def get_batch(self, code: str) -> e.BatchResult | None:
        return self._batches.get(code)

    # ----- 遥测（多版本） -----
    def upsert_telemetry(self, sample: e.TelemetrySample) -> None:
        key = (sample.link_code, sample.bucket_ts)
        versions = self._telemetry.setdefault(key, [])
        if sample.observed and versions:
            # 更正观测：旧版本标记失效并保留，新版本号递增
            for old in versions:
                old.superseded = True
            sample.version = max(v.version for v in versions) + 1
        versions.append(sample)

    def get_telemetry(self, link_code: str, bucket_ts: int) -> e.TelemetrySample | None:
        versions = self._telemetry.get((link_code, bucket_ts))
        if not versions:
            return None
        return versions[-1]

    def list_telemetry_versions(
        self, link_code: str, bucket_ts: int
    ) -> list[e.TelemetrySample]:
        return list(self._telemetry.get((link_code, bucket_ts), []))

    def list_telemetry(self, link_code: str) -> list[e.TelemetrySample]:
        current = [
            versions[-1]
            for (lc, _ts), versions in self._telemetry.items()
            if lc == link_code
        ]
        return sorted(current, key=lambda s: s.bucket_ts)

    # ----- 事件 -----
    def add_incident(self, incident: e.Incident) -> None:
        if incident.code in self._incidents:
            raise ConflictError(f"事件 {incident.code} 已存在")
        self._incidents[incident.code] = incident

    def get_incident(self, code: str) -> e.Incident | None:
        return self._incidents.get(code)

    def list_incidents(self, status: IncidentStatus | None = None) -> list[e.Incident]:
        rows = list(self._incidents.values())
        if status is not None:
            rows = [i for i in rows if i.status is status]
        return sorted(rows, key=lambda i: (i.starts_at, i.code))

    def update_incident(self, incident: e.Incident) -> None:
        self._incidents[incident.code] = incident

    # ----- 处置动作 -----
    def add_action(self, action: e.ReservationAction) -> None:
        self._actions.append(action)

    def list_actions(
        self, incident_code: str | None = None, reservation_code: str | None = None
    ) -> list[e.ReservationAction]:
        rows = self._actions
        if incident_code is not None:
            rows = [a for a in rows if a.incident_code == incident_code]
        if reservation_code is not None:
            rows = [a for a in rows if a.reservation_code == reservation_code]
        return sorted(rows, key=lambda a: (a.decided_at, a.code))

    # ----- 补偿 -----
    def get_compensation(self, idempotency_key: str) -> e.Compensation | None:
        versions = self._compensations.get(idempotency_key)
        return versions[-1] if versions else None

    def add_compensation(self, comp: e.Compensation) -> None:
        self._compensations.setdefault(comp.idempotency_key, []).append(comp)

    def update_compensation(self, comp: e.Compensation) -> None:
        versions = self._compensations.setdefault(comp.idempotency_key, [])
        for i, old in enumerate(versions):
            if old.code == comp.code:
                versions[i] = comp
                return
        versions.append(comp)

    def list_compensation_versions(self, idempotency_key: str) -> list[e.Compensation]:
        return list(self._compensations.get(idempotency_key, []))

    def list_compensations(self, incident_code: str | None = None) -> list[e.Compensation]:
        # 每个幂等键只返回最新版本，历史版本通过 list_compensation_versions 审计
        rows = [versions[-1] for versions in self._compensations.values()]
        if incident_code is not None:
            rows = [c for c in rows if c.incident_code == incident_code]
        return sorted(rows, key=lambda c: (c.window_start, c.code))

    # ----- 账本 -----
    def has_ledger_entry(self, idempotency_key: str) -> bool:
        return any(en.idempotency_key == idempotency_key for en in self._ledger)

    def add_ledger_entry(self, entry: e.LedgerEntry) -> None:
        if self.has_ledger_entry(entry.idempotency_key):
            raise ConflictError(f"账本幂等键 {entry.idempotency_key} 已过账")
        self._ledger.append(entry)

    def list_ledger(
        self, period: str | None = None, tenant_code: str | None = None
    ) -> list[e.LedgerEntry]:
        rows = self._ledger
        if period is not None:
            rows = [en for en in rows if en.period == period]
        if tenant_code is not None:
            rows = [en for en in rows if en.tenant_code == tenant_code]
        return sorted(rows, key=lambda en: (en.created_at, en.code))

    # ----- 账期 -----
    def get_or_create_period(self, period: str) -> e.AccountingPeriod:
        if period not in self._periods:
            self._periods[period] = e.AccountingPeriod(period=period)
        return self._periods[period]

    def update_period(self, period: e.AccountingPeriod) -> None:
        self._periods[period.period] = period

    def list_periods(self) -> list[e.AccountingPeriod]:
        return sorted(self._periods.values(), key=lambda p: p.period)
