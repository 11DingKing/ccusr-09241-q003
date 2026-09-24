"""业务策略常量与纯判定函数：SLO 违规、补偿费率、连续窗口阈值。"""

from __future__ import annotations

from ..domain.values import LatencyClass

# 退化认定：同一链路上连续观测到 SLO 违规的最少分钟数。
# 不足该连续窗口的瞬时抖动不触发事件，也不产生补偿。
MIN_CONSECUTIVE_DEGRADED = 3

# 补偿费率（元/分钟）：降级运行按带宽折价，中止按不可用计。
DEGRADED_RATE_PER_MINUTE = 2.0
DOWN_RATE_PER_MINUTE = 6.0


def money(value: float) -> float:
    return round(value + 1e-9, 2)


def sample_breaches_reservation(sample, reservation) -> bool:
    """该分钟观测是否违反预约原始 SLO（时延或可靠性任一）。

    使用预约的*原始*时延等级衡量：即使后续降级运行，
    补偿仍按客户最初购买的 SLO 判定违约分钟。
    """
    if not sample.observed or sample.superseded:
        return False
    max_ms = reservation.latency_class.max_latency_ms
    if (
        max_ms is not None
        and sample.observed_latency_ms is not None
        and sample.observed_latency_ms > max_ms + 1e-9
    ):
        return True
    if (
        sample.observed_reliability is not None
        and sample.observed_reliability + 1e-9 < reservation.reliability
    ):
        return True
    return False


def downgrade_class(latency: LatencyClass) -> LatencyClass:
    return latency.downgrade()
