"""运维处置服务。

已确认预约遇到链路故障、计划维护或遥测退化时：
  1. 找出受影响预约（事件窗口与预约窗口相交且承载路径含事件链路）；
  2. 按业务优先级从高到低分配有限的替代路径容量；
  3. 每条预约按 迁移 → 降级/中止 的次序处置：
       - 迁移成功（替代链路满足 SLO 且有余量）：影响 NONE，不产生补偿；
       - 故障/维护且无法迁移：控制类不可降级 → 中止；其余链路不可用也只能中止；
       - 遥测退化（链路仍在但 SLO 失守）：可降级类降级运行，控制类迁移或中止。
处置动作与归因全程留痕，供结算服务计算补偿。
"""

from __future__ import annotations

from ..domain import entities as e
from ..domain.values import (
    ActionKind,
    Impact,
    IncidentKind,
    IncidentStatus,
    LinkStatus,
    ReservationStatus,
)
from .errors import ConflictError, NotFoundError
from .policy import MIN_CONSECUTIVE_DEGRADED, downgrade_class, sample_breaches_reservation
from .ports import Clock, IdGenerator
from .repository import Repository
from .timeline import (
    contiguous_degraded_buckets,
    intersect,
    minute_bucket,
    overlaps,
)


class OperationsService:
    def __init__(self, repo: Repository, ids: IdGenerator, clock: Clock) -> None:
        self.repo = repo
        self.ids = ids
        self.clock = clock

    # ------------------------------------------------------------------
    # 事件登记
    # ------------------------------------------------------------------
    def report_failure(
        self, link_code: str, starts_at: int, ends_at: int | None = None, note: str = ""
    ) -> e.Incident:
        """登记链路硬故障并立即处置受影响预约。

        ends_at 给定时故障窗口事实完整（事件 CLOSED），否则保持 OPEN；
        链路是否恢复取决于当前时钟是否已越过结束时刻——未恢复时为 FAILED，
        之后可由 close_incident 在恢复时确认并重新置为 ACTIVE。
        """
        with self.repo.transaction():
            link = self.repo.get_link(link_code)
            if not link:
                raise NotFoundError(f"链路 {link_code} 不存在")
            now = self.clock.now()
            recovered = ends_at is not None and ends_at <= now
            incident = e.Incident(
                code=self.ids.next_code("INC"),
                kind=IncidentKind.FAILURE,
                link_code=link_code,
                starts_at=starts_at,
                ends_at=ends_at,
                status=IncidentStatus.CLOSED if ends_at is not None else IncidentStatus.OPEN,
                note=note or "链路硬故障",
                created_at=now,
                closed_at=now if ends_at is not None else None,
            )
            self.repo.add_incident(incident)
            link.status = LinkStatus.ACTIVE if recovered else LinkStatus.FAILED
            self.repo.save_link(link)
            affected = self._affected_reservations(incident)
            self._handle(incident, affected, reason_link_unavailable=True)
            return incident

    def activate_due_maintenance(self, at: int | None = None) -> list[e.Incident]:
        """将 starts_at <= at < ends_at 且尚未生成事件的维护窗口激活为事件。

        支持跨午夜窗口：窗口与预约的相交按半开区间秒级计算，与日期边界无关。
        """
        at = self.clock.now() if at is None else at
        opened: list[e.Incident] = []
        with self.repo.transaction():
            existing_links = {
                (i.link_code, i.starts_at, i.ends_at)
                for i in self.repo.list_incidents()
                if i.kind is IncidentKind.MAINTENANCE and i.status is not IncidentStatus.REVOKED
            }
            for window in self.repo.list_maintenance():
                if window.starts_at <= at < window.ends_at and (
                    window.link_code,
                    window.starts_at,
                    window.ends_at,
                ) not in existing_links:
                    incident = e.Incident(
                        code=self.ids.next_code("INC"),
                        kind=IncidentKind.MAINTENANCE,
                        link_code=window.link_code,
                        starts_at=window.starts_at,
                        ends_at=window.ends_at,
                        status=IncidentStatus.OPEN,
                        note=window.note or f"计划维护 {window.code}",
                        created_at=self.clock.now(),
                    )
                    self.repo.add_incident(incident)
                    affected = self._affected_reservations(incident)
                    self._handle(incident, affected, reason_link_unavailable=True)
                    opened.append(incident)
            return opened

    def detect_degradation(
        self, link_code: str, window_start: int, window_end: int
    ) -> dict:
        """依据连续观测窗口检测遥测退化并处置。

        仅当链路上某预约的 SLO 在至少 MIN_CONSECUTIVE_DEGRADED 个连续分钟桶
        （均有真实观测）被违反时才形成事件；瞬时抖动与数据缺口不算。
        返回新建事件与被既有事件覆盖而跳过的段。
        """
        with self.repo.transaction():
            link = self.repo.get_link(link_code)
            if not link:
                raise NotFoundError(f"链路 {link_code} 不存在")
            if window_end <= window_start:
                from .errors import ValidationError

                raise ValidationError("检测窗口结束时间必须晚于开始时间")

            samples = {s.bucket_ts: s for s in self.repo.list_telemetry(link_code)}
            reservations = [
                r
                for r in self.repo.list_reservations()
                if link_code in r.current_links
                and r.overlaps(window_start, window_end)
            ]

            # 每个预约各自的连续违约段，再合并为链路段事件
            raw_segments: list[tuple[int, int, list[e.Reservation]]] = []
            for res in reservations:
                part = intersect(res.starts_at, res.ends_at, window_start, window_end)
                if not part:
                    continue
                segments = contiguous_degraded_buckets(
                    samples,
                    part[0],
                    part[1],
                    lambda smp, rv=res: sample_breaches_reservation(smp, rv),
                    min_consecutive=MIN_CONSECUTIVE_DEGRADED,
                )
                for seg in segments:
                    raw_segments.append((seg[0], seg[1], [res]))

            merged = self._merge_segments(raw_segments)
            created: list[str] = []
            skipped: list[dict] = []
            for seg_start, seg_end, seg_reservations in merged:
                cover = self._existing_degradation(link_code, seg_start, seg_end)
                if cover is not None:
                    skipped.append(
                        {"segment": [seg_start, seg_end], "incident": cover.code}
                    )
                    continue
                evidence = [
                    str(ts)
                    for ts in range(minute_bucket(seg_start), seg_end, 60)
                    if (smp := samples.get(ts)) and smp.observed and not smp.superseded
                ]
                incident = e.Incident(
                    code=self.ids.next_code("INC"),
                    kind=IncidentKind.TELEMETRY_DEGRADATION,
                    link_code=link_code,
                    starts_at=seg_start,
                    ends_at=seg_end,
                    status=IncidentStatus.CLOSED,  # 历史窗口证据完整
                    note=(
                        f"链路 {link_code} 连续 {len(evidence)} 个观测分钟违反预约 SLO"
                    ),
                    evidence=evidence,
                    created_at=self.clock.now(),
                    closed_at=self.clock.now(),
                )
                self.repo.add_incident(incident)
                # 去重后的受影响预约
                uniq = {r.code: r for r in seg_reservations}
                self._handle(
                    incident,
                    list(uniq.values()),
                    reason_link_unavailable=False,
                    observed_segments={
                        r.code: self._reservation_segments(samples, r, seg_start, seg_end)
                        for r in uniq.values()
                    },
                )
                created.append(incident.code)
            return {"link": link_code, "created_incidents": created, "skipped": skipped}

    def close_incident(self, code: str, ends_at: int | None = None) -> e.Incident:
        """故障恢复/维护结束：关闭事件并收口所有处置动作。"""
        with self.repo.transaction():
            incident = self.repo.get_incident(code)
            if not incident:
                raise NotFoundError(f"事件 {code} 不存在")
            if incident.status is IncidentStatus.REVOKED:
                raise ConflictError(f"事件 {code} 已被更正撤销，不能关闭")
            if incident.kind is IncidentKind.FAILURE:
                # 关闭（或对已登记完整窗口的事件再次确认恢复）：链路重新可用
                link = self.repo.get_link(incident.link_code)
                if link and link.status is LinkStatus.FAILED:
                    link.status = LinkStatus.ACTIVE
                    self.repo.save_link(link)
            if incident.status is IncidentStatus.CLOSED:
                return incident
            end = ends_at or self.clock.now()
            if end <= incident.starts_at:
                from .errors import ValidationError

                raise ValidationError("事件结束时间必须晚于开始时间")
            incident.ends_at = end
            incident.status = IncidentStatus.CLOSED
            incident.closed_at = self.clock.now()
            self.repo.update_incident(incident)
            if incident.kind is IncidentKind.FAILURE:
                link = self.repo.get_link(incident.link_code)
                if link:
                    link.status = LinkStatus.ACTIVE
                    self.repo.save_link(link)
            for action in self.repo.list_actions(incident_code=code):
                if action.effective_to is None:
                    res = self.repo.get_reservation(action.reservation_code)
                    cap = res.ends_at if res else None
                    action.effective_to = min(end, cap) if cap else end
            return incident

    def expire_reservations(self, now: int | None = None) -> list[str]:
        """窗口已结束的预约置为 COMPLETED。"""
        now = self.clock.now() if now is None else now
        completed: list[str] = []
        with self.repo.transaction():
            for res in self.repo.list_reservations():
                if res.is_active and res.ends_at <= now:
                    res.status = ReservationStatus.COMPLETED
                    self.repo.update_reservation(res)
                    completed.append(res.code)
            return completed

    # ------------------------------------------------------------------
    # 处置决策
    # ------------------------------------------------------------------
    def _affected_reservations(self, incident: e.Incident) -> list[e.Reservation]:
        if incident.ends_at is not None:
            # 事实窗口完整（维护、已恢复故障）：处理窗口内全部相交预约
            end = incident.ends_at
            return [
                r
                for r in self.repo.list_reservations()
                if r.is_active
                and incident.link_code in r.current_links
                and r.overlaps(incident.starts_at, end)
            ]
        # 故障仍在持续、结束时刻未知：只处置当前已在进行的预约，
        # 未来才开始的预约不在此刻臆断，待故障恢复或再次评估时处理
        now = self.clock.now()
        return [
            r
            for r in self.repo.list_reservations()
            if r.is_active
            and incident.link_code in r.current_links
            and r.starts_at <= now < r.ends_at
        ]

    @staticmethod
    def _far_future() -> int:
        return 2_147_483_646

    def _handle(
        self,
        incident: e.Incident,
        affected: list[e.Reservation],
        *,
        reason_link_unavailable: bool,
        observed_segments: dict[str, list[tuple[int, int]]] | None = None,
    ) -> None:
        # 优先级高者先获得稀缺的替代路径容量；同级按开始时间、编码排序
        affected.sort(key=lambda r: (-int(r.priority), r.starts_at, r.code))
        # 已在本事件处置中临时占用替代链路的负载，参与后续预约的容量判定
        tentative: list[tuple[str, int, int, float]] = []
        end = incident.ends_at if incident.ends_at is not None else self._far_future()

        for res in affected:
            overlap = intersect(res.starts_at, res.ends_at, incident.starts_at, end)
            if not overlap:
                continue
            seg_start, seg_end = overlap
            alt = self._find_alternative(incident.link_code, res, seg_start, seg_end, tentative)

            if alt is not None:
                self._migrate(incident, res, incident.link_code, alt, seg_start, seg_end)
                tentative.append((alt, seg_start, seg_end, res.bandwidth_mbps))
                continue

            if not reason_link_unavailable and res.priority.degradable:
                # 遥测退化且可降级：降一档继续运行，违约分钟由观测证据结算
                self._degrade(
                    incident,
                    res,
                    (observed_segments or {}).get(res.code, [(seg_start, seg_end)]),
                )
                continue

            # 链路不可用且无法迁移，或控制类遇退化无替代：中止
            self._abort(incident, res, seg_start, seg_end, reason_link_unavailable)

    def _find_alternative(
        self,
        failed_link: str,
        res: e.Reservation,
        seg_start: int,
        seg_end: int,
        tentative: list[tuple[str, int, int, float]],
    ) -> str | None:
        source = self.repo.get_link(failed_link)
        candidates = source.alternative_codes if source else []
        for cand_code in candidates:
            cand = self.repo.get_link(cand_code)
            if not cand or cand.status is not LinkStatus.ACTIVE:
                continue
            ok, _why = cand.supports(res.latency_class, res.reliability, cand.status)
            if not ok:
                continue
            # 候选链路在影响段内不能有维护或未撤销事件
            blocked = False
            for mw in self.repo.list_maintenance(cand_code):
                if overlaps(seg_start, seg_end, mw.starts_at, mw.ends_at):
                    blocked = True
                    break
            if blocked:
                continue
            for inc in self.repo.list_incidents():
                if inc.link_code != cand_code or inc.status is IncidentStatus.REVOKED:
                    continue
                inc_end = inc.ends_at if inc.ends_at is not None else self._far_future()
                if overlaps(seg_start, seg_end, inc.starts_at, inc_end):
                    blocked = True
                    break
            if blocked:
                continue
            # 容量：既有有效占用 + 本批次已迁移负载
            used = 0.0
            for other in self.repo.list_reservations():
                if (
                    other.is_active
                    and other.code != res.code
                    and cand_code in other.current_links
                    and other.overlaps(seg_start, seg_end)
                ):
                    part = intersect(other.starts_at, other.ends_at, seg_start, seg_end)
                    if part:
                        used += other.bandwidth_mbps
            for lc, ts, te, bw in tentative:
                if lc == cand_code and overlaps(seg_start, seg_end, ts, te):
                    used += bw
            if used + res.bandwidth_mbps <= cand.total_capacity_mbps + 1e-9:
                return cand_code
        return None

    def _migrate(
        self,
        incident: e.Incident,
        res: e.Reservation,
        failed_link: str,
        alt_link: str,
        seg_start: int,
        seg_end: int,
    ) -> None:
        from_links = list(res.current_links)
        new_links = [alt_link if c == failed_link else c for c in res.current_links]
        res.current_links = new_links
        res.status = ReservationStatus.MIGRATED
        self.repo.update_reservation(res)
        action = e.ReservationAction(
            code=self.ids.next_code("ACT"),
            incident_code=incident.code,
            reservation_code=res.code,
            action=ActionKind.MIGRATED,
            decided_at=self.clock.now(),
            impact=Impact.NONE,
            effective_from=seg_start,
            effective_to=seg_end if incident.ends_at is not None else None,
            reason=f"事件 {incident.code}：链路 {failed_link} 不可用，按优先级迁移至 {alt_link}",
            from_links=from_links,
            to_links=list(new_links),
            from_latency=res.current_latency,
            to_latency=res.current_latency,
        )
        self.repo.add_action(action)

    def _degrade(
        self,
        incident: e.Incident,
        res: e.Reservation,
        segments: list[tuple[int, int]],
    ) -> None:
        old_latency = res.current_latency or res.latency_class
        new_latency = downgrade_class(old_latency)
        res.current_latency = new_latency
        res.status = ReservationStatus.DEGRADED
        self.repo.update_reservation(res)
        seg_start = min(s[0] for s in segments)
        seg_end = max(s[1] for s in segments)
        action = e.ReservationAction(
            code=self.ids.next_code("ACT"),
            incident_code=incident.code,
            reservation_code=res.code,
            action=ActionKind.DEGRADED,
            decided_at=self.clock.now(),
            impact=Impact.DEGRADED,
            effective_from=seg_start,
            effective_to=seg_end,
            reason=(
                f"事件 {incident.code}：连续观测窗口内 SLO 失守，"
                f"{res.priority.label}类可降级，{old_latency.value} → {new_latency.value}"
            ),
            from_links=list(res.current_links),
            to_links=list(res.current_links),
            from_latency=old_latency,
            to_latency=new_latency,
        )
        self.repo.add_action(action)

    def _abort(
        self,
        incident: e.Incident,
        res: e.Reservation,
        seg_start: int,
        seg_end: int,
        reason_link_unavailable: bool,
    ) -> None:
        res.status = ReservationStatus.ABORTED
        self.repo.update_reservation(res)
        why = (
            "链路不可用且无满足 SLO/容量的替代路径"
            if reason_link_unavailable
            else "控制类预约不可降级且无可用替代路径"
        )
        action = e.ReservationAction(
            code=self.ids.next_code("ACT"),
            incident_code=incident.code,
            reservation_code=res.code,
            action=ActionKind.ABORTED,
            decided_at=self.clock.now(),
            impact=Impact.ABORTED,
            effective_from=seg_start,
            effective_to=seg_end if incident.ends_at is not None else None,
            reason=f"事件 {incident.code}：{why}，按优先级规则中止",
            from_links=list(res.current_links),
            to_links=[],
            from_latency=res.current_latency,
            to_latency=None,
        )
        self.repo.add_action(action)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _merge_segments(
        raw: list[tuple[int, int, list[e.Reservation]]],
    ) -> list[tuple[int, int, list[e.Reservation]]]:
        if not raw:
            return []
        ordered = sorted(raw, key=lambda x: (x[0], x[1]))
        merged: list[tuple[int, int, list[e.Reservation]]] = []
        for start, end, rs in ordered:
            if merged and start <= merged[-1][1]:
                prev_start, prev_end, prev_rs = merged[-1]
                merged[-1] = (prev_start, max(prev_end, end), prev_rs + rs)
            else:
                merged.append((start, end, list(rs)))
        return merged

    def _existing_degradation(
        self, link_code: str, seg_start: int, seg_end: int
    ) -> e.Incident | None:
        for inc in self.repo.list_incidents():
            if (
                inc.kind is IncidentKind.TELEMETRY_DEGRADATION
                and inc.status is not IncidentStatus.REVOKED
                and inc.link_code == link_code
                and inc.starts_at <= seg_start
                and (inc.ends_at or 0) >= seg_end
            ):
                return inc
        return None

    def _reservation_segments(
        self, samples: dict[int, object], res: e.Reservation, start: int, end: int
    ) -> list[tuple[int, int]]:
        part = intersect(res.starts_at, res.ends_at, start, end)
        if not part:
            return []
        return contiguous_degraded_buckets(
            samples,
            part[0],
            part[1],
            lambda smp: sample_breaches_reservation(smp, res),
            min_consecutive=MIN_CONSECUTIVE_DEGRADED,
        )
