"""目录与基础数据服务：链路、租户、维护窗口、账期封账。"""

from __future__ import annotations

from ..domain import entities as e
from ..domain.values import LatencyClass, LinkStatus, PeriodStatus
from .errors import ConflictError, NotFoundError, ValidationError
from .ports import Clock, IdGenerator
from .repository import Repository
from .timeline import overlaps


class CatalogService:
    def __init__(self, repo: Repository, ids: IdGenerator, clock: Clock) -> None:
        self.repo = repo
        self.ids = ids
        self.clock = clock

    # ----- 链路 -----
    def register_link(
        self,
        code: str,
        name: str,
        total_capacity_mbps: float,
        supported_latency: str,
        reliability_target: float,
        alternative_codes: list[str] | None = None,
    ) -> e.Link:
        if total_capacity_mbps <= 0:
            raise ValidationError("链路容量必须为正数")
        if not 0 < reliability_target <= 1:
            raise ValidationError("可靠性目标必须位于 (0, 1] 区间")
        try:
            latency = LatencyClass(supported_latency)
        except ValueError:
            raise ValidationError(
                f"未知时延等级 {supported_latency!r}",
                {"allowed": [v.value for v in LatencyClass]},
            )
        with self.repo.transaction():
            if self.repo.get_link(code):
                raise ConflictError(f"链路 {code} 已存在")
            link = e.Link(
                code=code,
                name=name,
                total_capacity_mbps=float(total_capacity_mbps),
                supported_latency=latency,
                reliability_target=float(reliability_target),
                alternative_codes=list(dict.fromkeys(alternative_codes or [])),
            )
            self.repo.save_link(link)
            return link

    def set_link_status(self, code: str, status: str) -> e.Link:
        try:
            new_status = LinkStatus(status)
        except ValueError:
            raise ValidationError(f"未知链路状态 {status!r}")
        with self.repo.transaction():
            link = self.repo.get_link(code)
            if not link:
                raise NotFoundError(f"链路 {code} 不存在")
            link.status = new_status
            self.repo.save_link(link)
            return link

    # ----- 租户 -----
    def register_tenant(self, code: str, name: str, quota_mbps: float) -> e.Tenant:
        if quota_mbps < 0:
            raise ValidationError("租户配额不能为负")
        with self.repo.transaction():
            if self.repo.get_tenant(code):
                raise ConflictError(f"租户 {code} 已存在")
            tenant = e.Tenant(code=code, name=name, quota_mbps=float(quota_mbps))
            self.repo.save_tenant(tenant)
            return tenant

    # ----- 维护窗口 -----
    def schedule_maintenance(
        self, link_code: str, starts_at: int, ends_at: int, note: str = ""
    ) -> e.MaintenanceWindow:
        if not isinstance(starts_at, int) or not isinstance(ends_at, int):
            raise ValidationError("时间戳必须为整数秒")
        if ends_at <= starts_at:
            raise ValidationError("维护窗口结束时间必须晚于开始时间")
        with self.repo.transaction():
            link = self.repo.get_link(link_code)
            if not link:
                raise NotFoundError(f"链路 {link_code} 不存在")
            conflict = next(
                (
                    w
                    for w in self.repo.list_maintenance(link_code)
                    if overlaps(starts_at, ends_at, w.starts_at, w.ends_at)
                ),
                None,
            )
            if conflict:
                raise ConflictError(
                    f"链路 {link_code} 在该时段已有维护窗口 {conflict.code}",
                    {"existing_window": conflict.code},
                )
            window = e.MaintenanceWindow(
                code=self.ids.next_code("MW"),
                link_code=link_code,
                starts_at=starts_at,
                ends_at=ends_at,
                note=note,
            )
            self.repo.add_maintenance(window)
            return window

    # ----- 账期 -----
    def close_period(self, period: str) -> e.AccountingPeriod:
        with self.repo.transaction():
            acc = self.repo.get_or_create_period(period)
            if acc.status is PeriodStatus.CLOSED:
                raise ConflictError(f"账期 {period} 已封账")
            acc.status = PeriodStatus.CLOSED
            acc.closed_at = self.clock.now()
            self.repo.update_period(acc)
            return acc

    def ensure_period_open(self, period: str) -> e.AccountingPeriod:
        with self.repo.transaction():
            acc = self.repo.get_or_create_period(period)
            if acc.status is PeriodStatus.CLOSED:
                from .errors import PeriodClosedError

                raise PeriodClosedError(period)
            return acc
