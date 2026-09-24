"""结算与补偿服务。

核心不变量：
  * 补偿只依据处置动作（归因）与其有效区间内的连续观测证据计算；
  * 同一 (事件, 预约) 的补偿有唯一幂等键，重复结算返回同一结果，不重复过账；
  * 遥测更正后重算：旧版本补偿置 SUPERSEDED，差额以 ADJUSTMENT 计入*当前开放账期*；
    已封账月份永不被改写，只追加可审计的冲销/调整分录；
  * 事件被撤销（更正后违约不再成立）时，原补偿在当前开放账期全额冲销。
"""

from __future__ import annotations

from ..domain import entities as e
from ..domain.values import (
    ActionKind,
    CompensationStatus,
    EntryType,
    Impact,
    IncidentKind,
    IncidentStatus,
)
from .errors import NotFoundError, PeriodClosedError, ValidationError
from .policy import (
    DEGRADED_RATE_PER_MINUTE,
    DOWN_RATE_PER_MINUTE,
    MIN_CONSECUTIVE_DEGRADED,
    money,
    sample_breaches_reservation,
)
from .ports import Clock, IdGenerator
from .repository import Repository
from .timeline import (
    contiguous_degraded_buckets,
    intersect,
    period_of,
)


class SettlementService:
    def __init__(self, repo: Repository, ids: IdGenerator, clock: Clock) -> None:
        self.repo = repo
        self.ids = ids
        self.clock = clock

    # ------------------------------------------------------------------
    def settle_incident(self, incident_code: str) -> dict:
        """计算（如需要）并过账某事件下全部应补偿动作。重复调用结果一致。"""
        with self.repo.transaction():
            incident = self.repo.get_incident(incident_code)
            if not incident:
                raise NotFoundError(f"事件 {incident_code} 不存在")

            if incident.status is IncidentStatus.REVOKED:
                return self._reverse_revoked(incident)

            actions = self.repo.list_actions(incident_code=incident_code)
            compensable = [a for a in actions if a.impact is not Impact.NONE]
            # 事件尚未关闭（故障未恢复、维护进行中）时窗口事实不完整：
            # 存在待补偿动作则必须等关闭后再结算；仅迁移（无中断）可安全返回空。
            if incident.status is not IncidentStatus.CLOSED and compensable:
                from .errors import ConflictError

                raise ConflictError(
                    f"事件 {incident_code} 尚未关闭，处置区间不完整，不能结算；"
                    "请在故障恢复/维护结束关闭事件后重算",
                    {"incident": incident_code, "incident_status": incident.status.value},
                )

            postings: list[dict] = []
            for action in actions:
                if action.impact is Impact.NONE:
                    continue  # 成功迁移、无中断，不补偿
                comp = self._compute(incident, action)
                if comp is None:
                    continue
                result = self._post_or_adjust(incident, action, comp)
                if result:
                    postings.append(result)
            return {"incident": incident_code, "postings": postings}

    def settle_all(self) -> dict:
        """对全部已关闭、未撤销事件执行一遍结算（可安全重放）。"""
        with self.repo.transaction():
            codes = [
                i.code
                for i in self.repo.list_incidents()
                if i.status is IncidentStatus.CLOSED
            ]
        results = [self.settle_incident(code) for code in codes]
        return {"settled_incidents": codes, "results": results}

    def replay(self) -> dict:
        """结算重放：再次执行全量结算；账本不应发生任何变化。"""
        before = self._ledger_digest()
        out = self.settle_all()
        after = self._ledger_digest()
        out["replay"] = {
            "entries_before": before["count"],
            "entries_after": after["count"],
            "balance_before": before["balance"],
            "balance_after": after["balance"],
            "ledger_unchanged": before == after,
        }
        return out

    # ------------------------------------------------------------------
    def recompute_after_correction(
        self, link_code: str, window_start: int, window_end: int
    ) -> dict:
        """遥测更正后重算受影响时段。

        1) 重检该窗口：新违约段建事件；原退化事件若在新证据下不再成立则撤销；
        2) 对受影响事件重新结算：差额进当前开放账期，封账月份不动。
        """
        if window_end <= window_start:
            raise ValidationError("重算窗口结束时间必须晚于开始时间")
        with self.repo.transaction():
            if self.repo.get_link(link_code) is None:
                raise NotFoundError(f"链路 {link_code} 不存在")

            touched: set[str] = set()
            # 既有退化事件：用更正后的证据重新判断
            for incident in self.repo.list_incidents():
                if (
                    incident.kind is not IncidentKind.TELEMETRY_DEGRADATION
                    or incident.status is IncidentStatus.REVOKED
                    or incident.link_code != link_code
                ):
                    continue
                if not intersect(
                    incident.starts_at, incident.ends_at or window_end, window_start, window_end
                ):
                    continue
                touched.add(incident.code)
                if not self._still_supported(incident):
                    incident.status = IncidentStatus.REVOKED
                    incident.note = incident.note + "（遥测更正后重算：违约不再成立，事件撤销）"
                    self.repo.update_incident(incident)

            # 新证据下的检测（已被既有事件覆盖的段会自动跳过）
            # 为避免服务间循环依赖，这里内联调用 operations
            from .operations import OperationsService

            ops = OperationsService(self.repo, self.ids, self.clock)
            detection = ops.detect_degradation(link_code, window_start, window_end)
            touched.update(detection["created_incidents"])

            postings: list[dict] = []
            for code in sorted(touched):
                postings.append(self.settle_incident(code))
            return {
                "link": link_code,
                "window": [window_start, window_end],
                "revoked_or_kept": sorted(touched),
                "new_incidents": detection["created_incidents"],
                "settlements": postings,
            }

    # ------------------------------------------------------------------
    def _compute(
        self, incident: e.Incident, action: e.ReservationAction
    ) -> dict | None:
        """依据归因动作与连续观测证据计算补偿量。返回金额与分钟构成。"""
        res = self.repo.get_reservation(action.reservation_code)
        if not res:
            return None
        end = action.effective_to or incident.ends_at or res.ends_at
        window = intersect(action.effective_from, end, res.starts_at, res.ends_at)
        if not window:
            return None
        w_start, w_end = window

        degraded_minutes = 0
        down_minutes = 0

        if action.action is ActionKind.ABORTED:
            # 硬故障/维护的不可用：按处置区间整段计；无需遥测佐证
            down_minutes = max(0, (w_end - w_start) // 60)
        elif action.action is ActionKind.DEGRADED:
            # 降级：按区间内当前有效观测中、属于连续违约窗口的分钟数计
            samples = {s.bucket_ts: s for s in self.repo.list_telemetry(incident.link_code)}
            segments = contiguous_degraded_buckets(
                samples,
                w_start,
                w_end,
                lambda smp: sample_breaches_reservation(smp, res),
                min_consecutive=MIN_CONSECUTIVE_DEGRADED,
            )
            degraded_minutes = sum((e - s) // 60 for s, e in segments)

        amount = money(
            degraded_minutes * DEGRADED_RATE_PER_MINUTE
            + down_minutes * DOWN_RATE_PER_MINUTE
        )
        return {
            "amount": amount,
            "degraded_minutes": degraded_minutes,
            "down_minutes": down_minutes,
            "window_start": w_start,
            "window_end": w_end,
            "tenant_code": res.tenant_code,
            "attribution": (
                f"归因事件 {incident.code}（{incident.kind.value}）于链路 "
                f"{incident.link_code}；动作 {action.action.value}，"
                f"降级 {degraded_minutes} 分钟 / 中止 {down_minutes} 分钟"
            ),
        }

    def _post_or_adjust(
        self, incident: e.Incident, action: e.ReservationAction, calc: dict
    ) -> dict | None:
        key = f"{incident.code}:{action.reservation_code}"
        existing = self.repo.get_compensation(key)

        if existing is None:
            if calc["amount"] <= 0:
                return None  # 无违约分钟（如更正后），不建补偿
            return self._post_new(key, incident, action, calc)

        # 已有补偿：金额未变即幂等返回
        if abs(existing.amount - calc["amount"]) < 0.005:
            return {
                "idempotent": True,
                "compensation": existing.code,
                "amount": existing.amount,
                "ledger_entry": existing.ledger_entry_code,
            }

        # 金额变化：旧版本置 SUPERSEDED，差额计入当前开放账期
        existing.status = CompensationStatus.SUPERSEDED
        self.repo.update_compensation(existing)

        new_comp = e.Compensation(
            code=self.ids.next_code("CMP"),
            incident_code=incident.code,
            reservation_code=action.reservation_code,
            tenant_code=calc["tenant_code"],
            amount=calc["amount"],
            degraded_minutes=calc["degraded_minutes"],
            down_minutes=calc["down_minutes"],
            window_start=calc["window_start"],
            window_end=calc["window_end"],
            attribution=calc["attribution"] + f"（重算版本 v{existing.version + 1}）",
            idempotency_key=key,
            version=existing.version + 1,
            status=CompensationStatus.POSTED,
            created_at=self.clock.now(),
        )

        delta = money(calc["amount"] - existing.amount)
        current_period = period_of(self.clock.now())
        self._ensure_open(current_period)
        if abs(delta) >= 0.01:
            entry = e.LedgerEntry(
                code=self.ids.next_code("LED"),
                period=current_period,
                tenant_code=calc["tenant_code"],
                entry_type=EntryType.ADJUSTMENT,
                amount=delta,
                idempotency_key=f"adj:{key}:v{new_comp.version}",
                compensation_code=new_comp.code,
                created_at=self.clock.now(),
                note=(
                    f"遥测更正重算差额（原 {existing.code} 计 {existing.amount} 元，"
                    f"事件归属月 {period_of(action.effective_from)} 已封账则不改动）"
                ),
            )
            self.repo.add_ledger_entry(entry)
            new_comp.ledger_entry_code = entry.code
        self.repo.add_compensation(new_comp)
        return {
            "idempotent": False,
            "superseded": existing.code,
            "compensation": new_comp.code,
            "amount": new_comp.amount,
            "delta": delta,
            "period": current_period,
        }

    def _post_new(
        self, key: str, incident: e.Incident, action: e.ReservationAction, calc: dict
    ) -> dict:
        impact_period = period_of(action.effective_from)
        self._ensure_open(impact_period)
        comp = e.Compensation(
            code=self.ids.next_code("CMP"),
            incident_code=incident.code,
            reservation_code=action.reservation_code,
            tenant_code=calc["tenant_code"],
            amount=calc["amount"],
            degraded_minutes=calc["degraded_minutes"],
            down_minutes=calc["down_minutes"],
            window_start=calc["window_start"],
            window_end=calc["window_end"],
            attribution=calc["attribution"],
            idempotency_key=key,
            status=CompensationStatus.POSTED,
            created_at=self.clock.now(),
        )
        entry = e.LedgerEntry(
            code=self.ids.next_code("LED"),
            period=impact_period,
            tenant_code=calc["tenant_code"],
            entry_type=EntryType.COMPENSATION,
            amount=calc["amount"],
            idempotency_key=f"comp:{key}",
            compensation_code=comp.code,
            created_at=self.clock.now(),
            note=f"事件 {incident.code} 服务补偿",
        )
        self.repo.add_ledger_entry(entry)
        comp.ledger_entry_code = entry.code
        self.repo.add_compensation(comp)
        return {
            "idempotent": False,
            "compensation": comp.code,
            "amount": comp.amount,
            "period": impact_period,
            "ledger_entry": entry.code,
        }

    def _reverse_revoked(self, incident: e.Incident) -> dict:
        """事件撤销：把其已过账补偿在当前开放账期全额冲销。"""
        current_period = period_of(self.clock.now())
        postings: list[dict] = []
        for comp in self.repo.list_compensations(incident.code):
            if comp.status is CompensationStatus.SUPERSEDED:
                continue
            reversal_key = f"rev:{comp.idempotency_key}:v{comp.version}"
            if self.repo.has_ledger_entry(reversal_key):
                postings.append({"idempotent": True, "compensation": comp.code})
                continue
            self._ensure_open(current_period)
            entry = e.LedgerEntry(
                code=self.ids.next_code("LED"),
                period=current_period,
                tenant_code=comp.tenant_code,
                entry_type=EntryType.REVERSAL,
                amount=money(-comp.amount),
                idempotency_key=reversal_key,
                compensation_code=comp.code,
                created_at=self.clock.now(),
                note=(
                    f"事件 {incident.code} 经遥测更正撤销，"
                    f"冲销原补偿 {comp.code}（原归属月 {period_of(comp.window_start)} 不变）"
                ),
            )
            self.repo.add_ledger_entry(entry)
            comp.status = CompensationStatus.SUPERSEDED
            self.repo.update_compensation(comp)
            postings.append(
                {
                    "idempotent": False,
                    "reversed_compensation": comp.code,
                    "reversal_entry": entry.code,
                    "amount": entry.amount,
                    "period": current_period,
                }
            )
        return {"incident": incident.code, "revoked": True, "postings": postings}

    # ------------------------------------------------------------------
    def _still_supported(self, incident: e.Incident) -> bool:
        """更正后的证据是否仍支撑该退化事件（任一关联预约仍有连续违约段）。"""
        samples = {s.bucket_ts: s for s in self.repo.list_telemetry(incident.link_code)}
        end = incident.ends_at
        for action in self.repo.list_actions(incident_code=incident.code):
            res = self.repo.get_reservation(action.reservation_code)
            if not res:
                continue
            window = intersect(
                incident.starts_at,
                end if end is not None else res.ends_at,
                res.starts_at,
                res.ends_at,
            )
            if not window:
                continue
            segments = contiguous_degraded_buckets(
                samples,
                window[0],
                window[1],
                lambda smp: sample_breaches_reservation(smp, res),
                min_consecutive=MIN_CONSECUTIVE_DEGRADED,
            )
            if segments:
                return True
        return False

    def _ensure_open(self, period: str) -> None:
        acc = self.repo.get_or_create_period(period)
        if acc.status.value == "CLOSED":
            raise PeriodClosedError(period)

    def _ledger_digest(self) -> dict:
        entries = self.repo.list_ledger()
        return {
            "count": len(entries),
            "balance": money(sum(en.amount for en in entries)),
            "keys": sorted(en.idempotency_key for en in entries),
        }

    # ------------------------------------------------------------------
    def ledger_report(self, period: str | None = None, tenant_code: str | None = None) -> dict:
        entries = self.repo.list_ledger(period, tenant_code)
        by_period: dict[str, float] = {}
        for en in entries:
            by_period[en.period] = money(by_period.get(en.period, 0.0) + en.amount)
        periods = {
            p.period: p.status.value for p in self.repo.list_periods()
        }
        return {
            "entries": [
                {
                    "code": en.code,
                    "period": en.period,
                    "period_status": periods.get(en.period, "OPEN"),
                    "tenant": en.tenant_code,
                    "type": en.entry_type.value,
                    "amount": en.amount,
                    "idempotency_key": en.idempotency_key,
                    "compensation": en.compensation_code,
                    "note": en.note,
                }
                for en in entries
            ],
            "balance_by_period": by_period,
            "total_balance": money(sum(en.amount for en in entries)),
            "periods": periods,
        }
