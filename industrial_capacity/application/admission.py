"""原子化批量准入服务。

一个批量请求在同一事务内校验：
  * 任何一项不满足，整批拒绝（不写入任何预约），并逐项给出可解释原因；
  * 全部满足时整批接受，容量与配额的判定同时计入既有预约与同批已过检项。
事务由 Repository 的互斥锁串行化，因此并发批量彼此隔离，不会超卖。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain import entities as e
from ..domain.values import (
    BatchStatus,
    LatencyClass,
    LinkStatus,
    Priority,
    RejectCode,
    ReservationStatus,
)
from .errors import ValidationError
from .ports import Clock, IdGenerator
from .repository import Repository
from .timeline import overlaps, peak_concurrent_usage


@dataclass
class _Load:
    """容量判定用的负载记录（既有预约或同批候选项）。"""

    code: str
    tenant_code: str
    link_codes: list[str]
    starts_at: int
    ends_at: int
    bandwidth_mbps: float


class AdmissionService:
    def __init__(self, repo: Repository, ids: IdGenerator, clock: Clock) -> None:
        self.repo = repo
        self.ids = ids
        self.clock = clock

    def submit_batch(self, items: list[dict]) -> e.BatchResult:
        if not isinstance(items, list) or not items:
            raise ValidationError("批量预约至少包含一项")

        now = self.clock.now()
        with self.repo.transaction():
            batch_code = self.ids.next_code("BATCH")
            rejected: list[dict] = []
            tentative: list[_Load] = []  # 同批已过检项，参与后续项判定

            for index, raw in enumerate(items):
                reasons = self._validate_item(raw, tentative)
                if reasons:
                    rejected.append({"index": index, "reasons": reasons, "item": raw})
                else:
                    tentative.append(
                        _Load(
                            code=f"(pending-{index})",
                            tenant_code=raw["tenant_code"],
                            link_codes=list(raw["link_codes"]),
                            starts_at=raw["starts_at"],
                            ends_at=raw["ends_at"],
                            bandwidth_mbps=float(raw["bandwidth_mbps"]),
                        )
                    )

            if rejected:
                # 原子化：整批不落库
                result = e.BatchResult(
                    code=batch_code,
                    status=BatchStatus.REJECTED.value,
                    accepted=[],
                    rejected=rejected,
                    decided_at=now,
                )
                self.repo.save_batch(result)
                return result

            # 全部过检：正式建单（此时再校验一次链路存在性已在前面完成）
            accepted_codes: list[str] = []
            for raw in items:
                res = e.Reservation(
                    code=self.ids.next_code("RSV"),
                    tenant_code=raw["tenant_code"],
                    link_codes=list(raw["link_codes"]),
                    starts_at=raw["starts_at"],
                    ends_at=raw["ends_at"],
                    bandwidth_mbps=float(raw["bandwidth_mbps"]),
                    latency_class=LatencyClass(raw["latency_class"]),
                    reliability=float(raw["reliability"]),
                    priority=Priority(int(raw["priority"]))
                    if str(raw["priority"]).isdigit()
                    else Priority[raw["priority"]],
                    status=ReservationStatus.CONFIRMED,
                    batch_code=batch_code,
                    created_at=now,
                )
                self.repo.add_reservation(res)
                accepted_codes.append(res.code)

            result = e.BatchResult(
                code=batch_code,
                status=BatchStatus.ACCEPTED.value,
                accepted=accepted_codes,
                rejected=[],
                decided_at=now,
            )
            self.repo.save_batch(result)
            return result

    # ------------------------------------------------------------------
    def _validate_item(self, raw: dict, tentative: list[_Load]) -> list[dict]:
        reasons: list[dict] = []

        tenant_code = raw.get("tenant_code")
        link_codes = raw.get("link_codes")
        starts_at = raw.get("starts_at")
        ends_at = raw.get("ends_at")
        bandwidth = raw.get("bandwidth_mbps")
        latency_raw = raw.get("latency_class")
        reliability = raw.get("reliability")
        priority_raw = raw.get("priority", Priority.STANDARD.name)

        # 基础格式
        if not isinstance(starts_at, int) or not isinstance(ends_at, int) or ends_at <= starts_at:
            reasons.append(
                {
                    "code": RejectCode.WINDOW_INVALID,
                    "message": "预约窗口非法：开始/结束需为整数秒且结束晚于开始",
                }
            )
        if not isinstance(bandwidth, (int, float)) or bandwidth <= 0:
            reasons.append(
                {"code": RejectCode.WINDOW_INVALID, "message": "预留带宽必须为正数"}
            )

        # 时延等级与优先级（枚举非法按本项原因收集，不影响同批其他项的解释）
        latency: LatencyClass | None = None
        try:
            latency = LatencyClass(latency_raw)
        except (ValueError, TypeError):
            reasons.append(
                {
                    "code": RejectCode.BAD_REQUEST,
                    "message": f"未知时延等级 {latency_raw!r}",
                    "allowed": [v.value for v in LatencyClass],
                }
            )
        priority = Priority.STANDARD
        try:
            priority = (
                Priority(int(priority_raw))
                if str(priority_raw).isdigit()
                else Priority[priority_raw]
            )
        except (KeyError, TypeError):
            reasons.append(
                {
                    "code": RejectCode.BAD_REQUEST,
                    "message": f"未知业务优先级 {priority_raw!r}",
                    "allowed": [p.name for p in Priority],
                }
            )
        reliability_ok = isinstance(reliability, (int, float)) and 0 < reliability <= 1
        if not reliability_ok:
            reasons.append(
                {
                    "code": RejectCode.RELIABILITY_UNSUPPORTED,
                    "message": "可靠性目标必须位于 (0, 1] 区间",
                }
            )

        # 时间窗口非法则后续区间运算无法进行，本项原因到此为止
        if not isinstance(starts_at, int) or not isinstance(ends_at, int) or ends_at <= starts_at:
            return reasons

        # 租户
        tenant = self.repo.get_tenant(tenant_code) if tenant_code else None
        if not tenant_code:
            reasons.append({"code": RejectCode.BAD_REQUEST, "message": "缺少租户编码"})
        elif tenant is None:
            reasons.append(
                {
                    "code": RejectCode.TENANT_NOT_FOUND,
                    "message": f"租户 {tenant_code} 不存在",
                }
            )

        # 路径
        if not isinstance(link_codes, list) or not link_codes:
            reasons.append(
                {"code": RejectCode.EMPTY_PATH, "message": "预约路径至少包含一条链路"}
            )
            return reasons  # 后续校验依赖路径

        links: list[e.Link] = []
        missing = [c for c in link_codes if self.repo.get_link(c) is None]
        if missing:
            reasons.append(
                {
                    "code": RejectCode.LINK_NOT_FOUND,
                    "message": f"路径中存在未注册链路：{missing}",
                    "links": missing,
                }
            )
        else:
            links = [self.repo.get_link(c) for c in link_codes]

        if reasons:
            return reasons

        # 链路状态 / 能力 / 维护冲突
        for link in links:
            if link.status is LinkStatus.FAILED:
                reasons.append(
                    {
                        "code": RejectCode.LINK_FAILED,
                        "message": f"链路 {link.code} 处于故障状态，无法承接新预约",
                        "link": link.code,
                    }
                )
                continue
            ok, why = link.supports(latency, float(reliability), link.status)
            if not ok:
                code = (
                    RejectCode.LATENCY_UNSUPPORTED
                    if "时延" in why
                    else RejectCode.RELIABILITY_UNSUPPORTED
                )
                reasons.append(
                    {"code": code, "message": f"链路 {link.code}：{why}", "link": link.code}
                )
            for mw in self.repo.list_maintenance(link.code):
                if overlaps(starts_at, ends_at, mw.starts_at, mw.ends_at):
                    reasons.append(
                        {
                            "code": RejectCode.MAINTENANCE_CONFLICT,
                            "message": (
                                f"链路 {link.code} 在 {mw.starts_at}~{mw.ends_at} "
                                f"有计划维护（{mw.note or mw.code}），与预约窗口冲突"
                            ),
                            "link": link.code,
                            "maintenance_window": mw.code,
                            "conflict_at": [
                                max(starts_at, mw.starts_at),
                                min(ends_at, mw.ends_at),
                            ],
                        }
                    )

        # 容量：既有有效预约 + 同批已过检项
        existing_loads = [
            _Load(r.code, r.tenant_code, r.current_links, r.starts_at, r.ends_at, r.bandwidth_mbps)
            for r in self.repo.list_reservations()
            if r.is_active
        ]
        for link in links:
            used_on_link = [
                (ld.starts_at, ld.ends_at, ld.bandwidth_mbps)
                for ld in existing_loads + tentative
                if link.code in ld.link_codes
                and overlaps(starts_at, ends_at, ld.starts_at, ld.ends_at)
            ]
            used_on_link.append((starts_at, ends_at, float(bandwidth)))
            peak = peak_concurrent_usage(used_on_link)
            if peak > link.total_capacity_mbps + 1e-9:
                concurrent = sorted(
                    [
                        {"reservation": ld.code, "bandwidth_mbps": ld.bandwidth_mbps}
                        for ld in existing_loads + tentative
                        if link.code in ld.link_codes
                        and overlaps(starts_at, ends_at, ld.starts_at, ld.ends_at)
                    ],
                    key=lambda x: x["reservation"],
                )
                reasons.append(
                    {
                        "code": RejectCode.INSUFFICIENT_CAPACITY,
                        "message": (
                            f"链路 {link.code} 容量不足：窗口内峰值需求 "
                            f"{peak:g}Mbps 超过容量 {link.total_capacity_mbps:g}Mbps"
                        ),
                        "link": link.code,
                        "peak_required_mbps": round(peak, 6),
                        "capacity_mbps": link.total_capacity_mbps,
                        "concurrent": concurrent,
                    }
                )

        # 租户配额：任一时点并发预留总量（含本项与同批候选项）
        if tenant is not None:
            tenant_intervals = [
                (r.starts_at, r.ends_at, r.bandwidth_mbps)
                for r in self.repo.list_reservations()
                if r.is_active and r.tenant_code == tenant.code
            ]
            tenant_intervals.extend(
                (ld.starts_at, ld.ends_at, ld.bandwidth_mbps)
                for ld in tentative
                if ld.tenant_code == tenant.code
            )
            tenant_intervals.append((starts_at, ends_at, float(bandwidth)))
            peak_quota = peak_concurrent_usage(tenant_intervals)
            if peak_quota > tenant.quota_mbps + 1e-9:
                reasons.append(
                    {
                        "code": RejectCode.TENANT_QUOTA_EXCEEDED,
                        "message": (
                            f"租户 {tenant.code} 配额超限：并发预留峰值 "
                            f"{peak_quota:g}Mbps 超过配额 {tenant.quota_mbps:g}Mbps"
                        ),
                        "peak_required_mbps": round(peak_quota, 6),
                        "quota_mbps": tenant.quota_mbps,
                    }
                )

        _ = priority  # 优先级不影响准入本身，仅在受影响处置时使用
        return reasons

    def get_batch(self, code: str) -> e.BatchResult:
        result = self.repo.get_batch(code)
        if result is None:
            from .errors import NotFoundError

            raise NotFoundError(f"批量单 {code} 不存在")
        return result
