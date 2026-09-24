"""应用服务:用例编排与事务边界。

所有写路径在 ``store.lock``(可重入)内完成"判定 + 落库",
保证批量准入与处置在并发下的原子性;批次、台账、处置动作均以
业务键幂等,重放不改变结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..adapters.memory import InMemoryStore
from ..domain import admission as admission_domain
from ..domain import observation as observation_domain
from ..domain import remediation as remediation_domain
from ..domain import settlement as settlement_domain
from ..domain.errors import (
    ConflictError,
    NotFoundError,
    PeriodSealedError,
    REASON_INVALID_TIME_RANGE,
    ValidationError,
)
from ..domain.models import (
    AffectedReservation,
    BatchStatus,
    BreachRun,
    BreachWindow,
    ItemRejection,
    LedgerEntry,
    LedgerKind,
    Link,
    MaintenanceWindow,
    PeriodStatus,
    RemediationAction,
    RemediationKind,
    RemediationTrigger,
    Reservation,
    ReservationBatch,
    ReservationItem,
    ReservationStatus,
    SettlementPeriod,
    TelemetrySample,
    TenantQuota,
)
from ..domain.observation import ObservationConfig
from ..domain.remediation import PlannedKind
from ..domain.timeutil import format_instant, period_of, require_period, split_by_period
from .ports import Clock, IdGenerator


@dataclass(frozen=True)
class AppConfig:
    observation: ObservationConfig = field(default_factory=ObservationConfig)


# ---------------------------------------------------------------------------
# 目录:链路、配额、维护窗口
# ---------------------------------------------------------------------------
class CatalogService:
    def __init__(self, store: InMemoryStore, clock: Clock, id_gen: IdGenerator) -> None:
        self._store = store
        self._clock = clock
        self._id_gen = id_gen
        self._remediation: RemediationService | None = None  # 由容器注入

    def set_remediation(self, remediation: "RemediationService") -> None:
        self._remediation = remediation

    def create_link(
        self,
        *,
        name: str,
        capacity_mbps: int,
        latency_class,
        reliability_target: float,
        workshops: tuple[str, ...] = (),
        link_id: str | None = None,
    ) -> Link:
        if capacity_mbps <= 0:
            raise ValidationError("链路容量必须为正")
        if not 0 < reliability_target <= 1:
            raise ValidationError("可靠性目标必须在 (0, 1] 区间")
        link = Link(
            link_id=link_id or self._id_gen.new_id("lnk"),
            name=name,
            capacity_mbps=capacity_mbps,
            latency_class=latency_class,
            reliability_target=reliability_target,
            workshops=workshops,
        )
        with self._store.lock:
            if link.link_id in self._store.links:
                raise ConflictError(f"链路 {link.link_id} 已存在")
            self._store.links[link.link_id] = link
        return link

    def list_links(self) -> list[Link]:
        with self._store.lock:
            return sorted(self._store.links.values(), key=lambda l: l.link_id)

    def list_maintenance_windows(self, link_id: str | None = None) -> list[MaintenanceWindow]:
        with self._store.lock:
            windows = sorted(self._store.maintenance.values(), key=lambda w: (w.link_id, w.start))
            if link_id is not None:
                windows = [w for w in windows if w.link_id == link_id]
            return windows

    def set_quota(self, *, tenant_id: str, max_mbps: int, max_active_reservations: int) -> TenantQuota:
        if max_mbps <= 0 or max_active_reservations <= 0:
            raise ValidationError("配额必须为正")
        quota = TenantQuota(
            tenant_id=tenant_id,
            max_mbps=max_mbps,
            max_active_reservations=max_active_reservations,
        )
        with self._store.lock:
            self._store.quotas[tenant_id] = quota
        return quota

    def create_maintenance_window(
        self,
        *,
        link_id: str,
        start: datetime,
        end: datetime,
        kind,
        available_mbps: int = 0,
        window_id: str | None = None,
    ) -> tuple[MaintenanceWindow, list[RemediationAction]]:
        with self._store.lock:
            link = self._store.links.get(link_id)
            if link is None:
                raise NotFoundError(f"链路 {link_id} 不存在")
            if end <= start:
                raise ValidationError("维护窗口结束时间必须晚于开始时间")
            window = MaintenanceWindow(
                window_id=window_id or self._id_gen.new_id("mw"),
                link_id=link_id,
                start=start,
                end=end,
                kind=kind,
                available_mbps=available_mbps,
            )
            self._store.maintenance[window.window_id] = window
            # 维护窗口冲击已确认预约:按业务优先级执行迁移/降级/中止
            actions = self._remediation.handle_maintenance(window) if self._remediation else []
        return window, actions


# ---------------------------------------------------------------------------
# 准入:批量预约的原子化受理
# ---------------------------------------------------------------------------
class AdmissionService:
    def __init__(self, store: InMemoryStore, clock: Clock, id_gen: IdGenerator) -> None:
        self._store = store
        self._clock = clock
        self._id_gen = id_gen

    def submit_batch(
        self,
        *,
        tenant_id: str,
        items: list[ReservationItem],
        batch_id: str | None = None,
    ) -> ReservationBatch:
        if not items:
            raise ValidationError("批量预约至少包含一条请求")
        batch_id = batch_id or self._id_gen.new_id("batch")
        with self._store.lock:
            existing = self._store.batches.get(batch_id)
            if existing is not None:
                return existing  # 幂等重放:同一 batch_id 返回首次判定结果

            now = self._clock.now()
            early_rejections = [
                ItemRejection(item.item_id, REASON_INVALID_TIME_RANGE, "开始时间不能早于当前时间")
                for item in items
                if item.start < now
            ]
            evaluable = [item for item in items if item.start >= now]
            accepted, rejections = admission_domain.evaluate_batch(
                evaluable,
                tenant_id=tenant_id,
                links=self._store.links,
                maintenance=list(self._store.maintenance.values()),
                occupying=self._store.occupying_reservations(),
                quota=self._store.quotas.get(tenant_id),
            )
            rejections = early_rejections + rejections

            if rejections:
                batch = ReservationBatch(
                    batch_id=batch_id,
                    tenant_id=tenant_id,
                    status=BatchStatus.REJECTED,
                    reservation_ids=(),
                    rejections=tuple(rejections),
                )
                self._store.batches[batch_id] = batch
                return batch

            reservation_ids: list[str] = []
            for item in accepted:
                reservation = Reservation(
                    reservation_id=self._id_gen.new_id("rsv"),
                    batch_id=batch_id,
                    tenant_id=tenant_id,
                    link_id=item.link_id,
                    start=item.start,
                    end=item.end,
                    bandwidth_mbps=item.bandwidth_mbps,
                    latency_class=item.latency_class,
                    business_priority=item.business_priority,
                )
                self._store.reservations[reservation.reservation_id] = reservation
                reservation_ids.append(reservation.reservation_id)
            batch = ReservationBatch(
                batch_id=batch_id,
                tenant_id=tenant_id,
                status=BatchStatus.CONFIRMED,
                reservation_ids=tuple(reservation_ids),
                rejections=(),
            )
            self._store.batches[batch_id] = batch
            return batch

    def get_batch(self, batch_id: str) -> ReservationBatch:
        with self._store.lock:
            batch = self._store.batches.get(batch_id)
            if batch is None:
                raise NotFoundError(f"批次 {batch_id} 不存在")
            return batch

    def get_reservation(self, reservation_id: str) -> Reservation:
        with self._store.lock:
            reservation = self._store.reservations.get(reservation_id)
            if reservation is None:
                raise NotFoundError(f"预约 {reservation_id} 不存在")
            return reservation

    def list_reservations(self, tenant_id: str | None = None) -> list[Reservation]:
        with self._store.lock:
            reservations = sorted(self._store.reservations.values(), key=lambda r: r.reservation_id)
            if tenant_id is not None:
                reservations = [r for r in reservations if r.tenant_id == tenant_id]
            return reservations


# ---------------------------------------------------------------------------
# 处置:维护/退化事件的迁移、降级、中止
# ---------------------------------------------------------------------------
class RemediationService:
    def __init__(self, store: InMemoryStore, clock: Clock, id_gen: IdGenerator) -> None:
        self._store = store
        self._clock = clock
        self._id_gen = id_gen

    def handle_maintenance(self, window: MaintenanceWindow) -> list[RemediationAction]:
        with self._store.lock:
            affected = [
                r
                for r in self._store.occupying_reservations()
                if r.link_id == window.link_id and r.start < window.end and r.end > window.start
            ]
            if not affected:
                return []
            return self._apply(
                RemediationTrigger.MAINTENANCE,
                event_key=window.window_id,
                affected=affected,
                capacity_override_by_link={},
            )

    def handle_degradation(
        self, link_id: str, run_start: datetime, worst_available_mbps: int
    ) -> list[RemediationAction]:
        with self._store.lock:
            affected = [
                r for r in self._store.occupying_reservations() if r.link_id == link_id
            ]
            if not affected:
                return []
            return self._apply(
                RemediationTrigger.DEGRADATION,
                event_key=f"run:{format_instant(run_start)}",
                affected=affected,
                capacity_override_by_link={link_id: worst_available_mbps},
            )

    def _apply(
        self,
        trigger: RemediationTrigger,
        *,
        event_key: str,
        affected: list[Reservation],
        capacity_override_by_link: dict[str, int],
    ) -> list[RemediationAction]:
        plans = remediation_domain.plan_remediation(
            affected,
            links=self._store.links,
            maintenance=list(self._store.maintenance.values()),
            occupying=self._store.occupying_reservations(),
            horizon_start=self._clock.now(),
            capacity_override_by_link=capacity_override_by_link,
        )
        applied: list[RemediationAction] = []
        for plan in plans:
            if plan.kind is PlannedKind.NONE:
                continue
            dedup_key = f"{trigger.value}|{event_key}|{plan.reservation_id}"
            if dedup_key in self._store.actions:
                continue  # 重复评估不重复执行
            reservation = self._store.reservations[plan.reservation_id]
            if not reservation.status.occupies_capacity:
                continue
            kind = remediation_domain.to_remediation_kind(plan.kind)
            if plan.kind is PlannedKind.MIGRATE:
                reservation.link_id = plan.detail["to_link_id"]
                reservation.status = ReservationStatus.MIGRATED
            elif plan.kind is PlannedKind.DEGRADE:
                reservation.bandwidth_mbps = plan.detail["to_bandwidth_mbps"]
                reservation.status = ReservationStatus.DEGRADED
            elif plan.kind is PlannedKind.TERMINATE:
                reservation.status = ReservationStatus.TERMINATED
                reservation.terminated_at = self._clock.now()
            reservation.version += 1
            action = RemediationAction(
                action_id=self._id_gen.new_id("act"),
                reservation_id=plan.reservation_id,
                trigger=trigger,
                kind=kind,
                detail=dict(plan.detail),
                dedup_key=dedup_key,
                created_at=self._clock.now(),
            )
            self._store.actions[dedup_key] = action
            applied.append(action)
        return applied

    def list_actions(self, reservation_id: str | None = None) -> list[RemediationAction]:
        with self._store.lock:
            actions = sorted(self._store.actions.values(), key=lambda a: a.action_id)
            if reservation_id is not None:
                actions = [a for a in actions if a.reservation_id == reservation_id]
            return actions


# ---------------------------------------------------------------------------
# 遥测:样本摄取(幂等)、连续窗口识别、历史更正与重算
# ---------------------------------------------------------------------------
class TelemetryService:
    def __init__(
        self,
        store: InMemoryStore,
        clock: Clock,
        id_gen: IdGenerator,
        config: AppConfig,
    ) -> None:
        self._store = store
        self._clock = clock
        self._id_gen = id_gen
        self._config = config
        self._remediation: RemediationService | None = None
        self._settlement: SettlementService | None = None

    def wire(self, remediation: "RemediationService", settlement: "SettlementService") -> None:
        self._remediation = remediation
        self._settlement = settlement

    # ---- 摄取 ----
    def ingest_samples(self, samples: list[TelemetrySample]) -> dict:
        accepted: list[str] = []
        duplicates: list[str] = []
        rejected: list[dict] = []
        touched_links: set[str] = set()
        with self._store.lock:
            for sample in sorted(samples, key=lambda s: s.ts):
                if sample.sample_id in self._store.samples:
                    duplicates.append(sample.sample_id)
                    continue
                if sample.link_id not in self._store.links:
                    rejected.append(
                        {"sample_id": sample.sample_id, "reason": f"链路 {sample.link_id} 不存在"}
                    )
                    continue
                self._store.add_sample(sample)
                accepted.append(sample.sample_id)
                touched_links.add(sample.link_id)
            for link_id in sorted(touched_links):
                self._refresh_link_observation(link_id)
        return {"accepted": len(accepted), "duplicates": len(duplicates), "rejected": rejected}

    # ---- 历史状态还原:违约判定与快照基于"应然需求",不受后续处置影响 ----
    def _historical_state(self, reservation: Reservation, t: datetime) -> tuple[str, int, bool]:
        """还原预约在时刻 t 的 (链路, 带宽, 是否在约)。

        处置动作只影响其生效时刻之后的状态;向过去回看时按动作记录逆向还原。
        """
        link_id = reservation.link_id
        bandwidth = reservation.bandwidth_mbps
        occupying = reservation.status.occupies_capacity
        actions = [
            a
            for a in self._store.actions.values()
            if a.reservation_id == reservation.reservation_id
        ]
        for action in sorted(actions, key=lambda a: a.created_at, reverse=True):
            if action.created_at >= t:
                # 动作是对时刻 t 观测的响应,对 t 时刻及其之前的判定不生效
                if action.kind is RemediationKind.DEGRADE:
                    bandwidth = action.detail.get("from_bandwidth_mbps", bandwidth)
                elif action.kind is RemediationKind.MIGRATE:
                    link_id = action.detail.get("from_link_id", link_id)
                elif action.kind is RemediationKind.TERMINATE:
                    occupying = True
        return link_id, bandwidth, occupying

    def _committed_demand_at(self, link_id: str, t: datetime) -> int:
        total = 0
        for reservation in self._store.reservations.values():
            r_link, r_bandwidth, r_occupying = self._historical_state(reservation, t)
            if r_occupying and r_link == link_id and reservation.start <= t < reservation.end:
                total += r_bandwidth
        return total

    def _snapshot_affected(
        self, link_id: str, start: datetime, end: datetime
    ) -> tuple[AffectedReservation, ...]:
        result: list[AffectedReservation] = []
        for reservation in self._store.reservations.values():
            r_link, r_bandwidth, r_occupying = self._historical_state(reservation, start)
            if (
                r_occupying
                and r_link == link_id
                and reservation.start < end
                and reservation.end > start
            ):
                result.append(
                    AffectedReservation(
                        reservation_id=reservation.reservation_id,
                        tenant_id=reservation.tenant_id,
                        bandwidth_mbps=r_bandwidth,
                        business_priority=reservation.business_priority,
                        start=reservation.start,
                        end=reservation.end,
                    )
                )
        return tuple(result)

    def _window_from_record(self, record: BreachRun) -> BreachWindow:
        config = self._config.observation
        end = record.last_ts + timedelta(minutes=config.sample_interval_minutes)
        maintenance = [
            w for w in self._store.maintenance.values() if w.link_id == record.link_id
        ]
        return BreachWindow(
            window_id=observation_domain.window_identity(record.link_id, record.start, end),
            link_id=record.link_id,
            start=record.start,
            end=end,
            committed_mbps=record.committed_peak_mbps,
            worst_available_mbps=record.worst_available_mbps,
            attribution=observation_domain.attribute_window(
                record.link_id, record.start, end, maintenance
            ),
            affected=record.affected,
        )

    def _refresh_link_observation(
        self,
        link_id: str,
        preserved: dict[str, tuple[AffectedReservation, ...]] | None = None,
    ) -> None:
        """根据最新样本流刷新该链路的违约序列与闭合窗口。"""
        config = self._config.observation
        interval = timedelta(minutes=config.sample_interval_minutes)
        stream = self._store.samples_for_link(link_id)
        maintenance = [w for w in self._store.maintenance.values() if w.link_id == link_id]
        runs = observation_domain.derive_runs(
            link_id, stream, lambda t: self._committed_demand_at(link_id, t), config
        )
        qualifying = [r for r in runs if observation_domain.run_qualifies(r, config)]

        # 1) 已确认的进行中序列:继续跟踪、闭合或按记录固化
        record = self._store.runs.get(link_id)
        handled_open_start: datetime | None = None
        if record is not None:
            matching_open = next(
                (r for r in qualifying if not r.closed and r.start == record.start), None
            )
            matching_closed = next(
                (r for r in qualifying if r.closed and r.start == record.start), None
            )
            if matching_open is not None:
                record.last_ts = matching_open.last_ts
                record.committed_peak_mbps = matching_open.committed_peak_mbps
                record.worst_available_mbps = matching_open.worst_available_mbps
                handled_open_start = record.start
            else:
                # 闭合(含处置生效后需求被满足、断档等):以确认时的记录固化窗口
                if matching_closed is not None:
                    record.last_ts = matching_closed.last_ts
                    record.committed_peak_mbps = matching_closed.committed_peak_mbps
                    record.worst_available_mbps = matching_closed.worst_available_mbps
                window = self._window_from_record(record)
                self._store.windows.setdefault(window.window_id, window)
                del self._store.runs[link_id]
                record = None

        # 2) 样本流中新闭合的窗口(无进行中记录对应,如批量补录)
        for run in qualifying:
            if not run.closed:
                continue
            end = run.last_ts + interval
            window_id = observation_domain.window_identity(link_id, run.start, end)
            if window_id in self._store.windows:
                continue
            if preserved and window_id in preserved:
                affected = preserved[window_id]
            else:
                affected = self._snapshot_affected(link_id, run.start, end)
            self._store.windows[window_id] = observation_domain.close_run(
                link_id, run, config=config, maintenance=maintenance, affected=affected
            )

        # 3) 新出现的进行中序列:确认即采集快照并触发处置
        for run in qualifying:
            if run.closed or run.start == handled_open_start:
                continue
            if self._store.runs.get(link_id) is not None:
                continue  # 已有进行中序列(理论上一链路至多一条)
            end = run.last_ts + interval
            self._store.runs[link_id] = BreachRun(
                link_id=link_id,
                start=run.start,
                last_ts=run.last_ts,
                committed_peak_mbps=run.committed_peak_mbps,
                worst_available_mbps=run.worst_available_mbps,
                affected=self._snapshot_affected(link_id, run.start, end),
            )
            # 连续违约确认:立即按业务优先级处置在约流量
            if self._remediation is not None:
                self._remediation.handle_degradation(
                    link_id, run.start, run.worst_available_mbps
                )

    # ---- 历史更正 ----
    def correct_samples(self, corrections: list[TelemetrySample]) -> dict:
        applied: list[str] = []
        duplicates: list[str] = []
        rejected: list[dict] = []
        touched_links: set[str] = set()
        with self._store.lock:
            for correction in corrections:
                if correction.sample_id in self._store.samples:
                    duplicates.append(correction.sample_id)
                    continue
                if not correction.corrects_sample_id:
                    rejected.append(
                        {"sample_id": correction.sample_id, "reason": "更正样本必须指明被更正样本"}
                    )
                    continue
                original = self._store.samples.get(correction.corrects_sample_id)
                if original is None:
                    rejected.append(
                        {
                            "sample_id": correction.sample_id,
                            "reason": f"被更正样本 {correction.corrects_sample_id} 不存在",
                        }
                    )
                    continue
                if original.link_id != correction.link_id or original.ts != correction.ts:
                    rejected.append(
                        {
                            "sample_id": correction.sample_id,
                            "reason": "更正样本的链路与时刻必须与被更正样本一致",
                        }
                    )
                    continue
                self._store.add_sample(correction)
                applied.append(correction.sample_id)
                touched_links.add(correction.link_id)

            affected_periods: set[str] = set()
            for link_id in sorted(touched_links):
                # 保留既有窗口快照:窗口身份未变的,受影响预约快照沿用首次确认时的记录
                preserved = {
                    w.window_id: w.affected
                    for w in self._store.windows.values()
                    if w.link_id == link_id
                }
                for window in list(self._store.windows.values()):
                    if window.link_id == link_id:
                        affected_periods.update(
                            p for p, _, _ in split_by_period(window.start, window.end)
                        )
                        self._store.windows_history[window.window_id] = window
                        del self._store.windows[window.window_id]
                self._store.runs.pop(link_id, None)
                self._refresh_link_observation(link_id, preserved=preserved)
                for window in self._store.windows.values():
                    if window.link_id == link_id:
                        affected_periods.update(
                            p for p, _, _ in split_by_period(window.start, window.end)
                        )

            reconciliation = {}
            if self._settlement is not None:
                for period in sorted(affected_periods):
                    reconciliation[period] = self._settlement.reconcile_period(period)
        return {
            "applied": len(applied),
            "duplicates": len(duplicates),
            "rejected": rejected,
            "reconciled_periods": reconciliation,
        }

    def list_windows(self, link_id: str | None = None) -> list[BreachWindow]:
        with self._store.lock:
            windows = sorted(self._store.windows.values(), key=lambda w: (w.start, w.window_id))
            if link_id is not None:
                windows = [w for w in windows if w.link_id == link_id]
            return windows


# ---------------------------------------------------------------------------
# 结算:账期调和、执行与封账
# ---------------------------------------------------------------------------
class SettlementService:
    def __init__(
        self,
        store: InMemoryStore,
        clock: Clock,
        id_gen: IdGenerator,
        config: AppConfig,
    ) -> None:
        self._store = store
        self._clock = clock
        self._id_gen = id_gen
        self._config = config

    def _ensure_period(self, period: str) -> SettlementPeriod:
        record = self._store.periods.get(period)
        if record is None:
            record = SettlementPeriod(period=period)
            self._store.periods[period] = record
        return record

    def run(self, period: str) -> dict:
        """执行账期结算:闭合到期窗口并按当前观测调和台账,重放安全。"""
        require_period(period)
        with self._store.lock:
            record = self._store.periods.get(period)
            if record is not None and record.status is PeriodStatus.SEALED:
                raise PeriodSealedError(f"账期 {period} 已封账,不能重复结算")
            self._finalize_due_runs()
            outcome = self.reconcile_period(period)
            record = self._ensure_period(period)
            record.run_count += 1
            replay = (
                outcome.get("created", 0) == 0
                and outcome.get("updated", 0) == 0
                and outcome.get("removed", 0) == 0
                and outcome.get("adjustments", 0) == 0
            )
            return {
                "period": period,
                "run_count": record.run_count,
                **outcome,
                "replay": replay,
            }

    def _finalize_due_runs(self) -> None:
        """把已超过一个采样间隔未更新的进行中序列闭合为窗口。"""
        interval = timedelta(minutes=self._config.observation.sample_interval_minutes)
        now = self._clock.now()
        for link_id, run in list(self._store.runs.items()):
            if now <= run.last_ts + interval:
                continue
            end = run.last_ts + interval
            window_id = observation_domain.window_identity(link_id, run.start, end)
            if window_id not in self._store.windows:
                maintenance = [
                    w for w in self._store.maintenance.values() if w.link_id == link_id
                ]
                self._store.windows[window_id] = BreachWindow(
                    window_id=window_id,
                    link_id=link_id,
                    start=run.start,
                    end=end,
                    committed_mbps=run.committed_peak_mbps,
                    worst_available_mbps=run.worst_available_mbps,
                    attribution=observation_domain.attribute_window(
                        link_id, run.start, end, maintenance
                    ),
                    affected=run.affected,
                )
            del self._store.runs[link_id]

    def reconcile_period(self, period: str) -> dict:
        """按当前窗口集合调和账期台账。

        开放账期:失效条目删除、缺失条目补齐,内容一致时无操作;
        已封账账期:封账条目保持不动,差额以调整条目计入当前开放账期。
        """
        with self._store.lock:
            desired = settlement_domain.compute_credit_entries(
                list(self._store.windows.values()), period
            )
            record = self._store.periods.get(period)
            sealed = record is not None and record.status is PeriodStatus.SEALED
            if not sealed:
                existing = [
                    e
                    for e in self._store.ledger.values()
                    if e.period == period and e.kind is LedgerKind.BREACH_CREDIT
                ]
                to_upsert, to_delete = settlement_domain.reconcile_open_period(desired, existing)
                existing_keys = {e.dedup_key for e in existing}
                created = sum(1 for e in to_upsert if e.dedup_key not in existing_keys)
                updated = sum(1 for e in to_upsert if e.dedup_key in existing_keys)
                for entry in to_delete:
                    self._store.ledger.pop(entry.dedup_key, None)
                for entry in to_upsert:
                    self._store.ledger[entry.dedup_key] = entry
                return {
                    "created": created,
                    "updated": updated,
                    "removed": len(to_delete),
                    "adjustments": 0,
                    "sealed": False,
                }

            recorded = [
                e
                for e in self._store.ledger.values()
                if (e.period == period and e.kind is LedgerKind.BREACH_CREDIT)
                or (e.kind is LedgerKind.CORRECTION_ADJUSTMENT and e.target_period == period)
            ]
            current_period = period_of(self._clock.now())
            current_record = self._store.periods.get(current_period)
            if current_record is not None and current_record.status is PeriodStatus.SEALED:
                raise ConflictError(f"当前账期 {current_period} 已封账,无开放账期承接调整")
            adjustments = settlement_domain.compute_adjustments(
                desired,
                recorded,
                target_period=period,
                current_period=current_period,
                now=self._clock.now(),
            )
            created = 0
            for entry in adjustments:
                if entry.dedup_key not in self._store.ledger:
                    self._store.ledger[entry.dedup_key] = entry
                    created += 1
            return {"created": 0, "removed": 0, "adjustments": created, "sealed": True}

    def seal(self, period: str) -> SettlementPeriod:
        require_period(period)
        with self._store.lock:
            record = self._ensure_period(period)
            if record.status is PeriodStatus.SEALED:
                raise ConflictError(f"账期 {period} 已封账")
            record.status = PeriodStatus.SEALED
            record.sealed_at = self._clock.now()
            return record

    def get_period(self, period: str) -> SettlementPeriod:
        require_period(period)
        with self._store.lock:
            record = self._store.periods.get(period)
            if record is None:
                raise NotFoundError(f"账期 {period} 不存在")
            return record

    def list_periods(self) -> list[SettlementPeriod]:
        with self._store.lock:
            return sorted(self._store.periods.values(), key=lambda p: p.period)

    def list_ledger(
        self, *, tenant_id: str | None = None, period: str | None = None
    ) -> list[LedgerEntry]:
        with self._store.lock:
            return self._store.ledger_entries(period=period, tenant_id=tenant_id)
