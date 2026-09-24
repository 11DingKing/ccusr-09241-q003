"""遥测接入与更正服务。

观测以一分钟桶聚合；同一桶的更正以新版本写入，旧版本标记失效但保留可审计。
是否构成退化由 OperationsService 依据连续观测窗口判定，本服务只负责证据管理。
"""

from __future__ import annotations

from ..domain import entities as e
from .errors import NotFoundError, ValidationError
from .ports import Clock
from .repository import Repository
from .timeline import BUCKET_SECONDS, minute_bucket


class TelemetryService:
    def __init__(self, repo: Repository, clock: Clock) -> None:
        self.repo = repo
        self.clock = clock

    def ingest(
        self,
        link_code: str,
        bucket_ts: int,
        observed_latency_ms: float | None,
        observed_reliability: float | None,
    ) -> e.TelemetrySample:
        """写入一个观测分钟桶（自动对齐到分钟起点）。"""
        with self.repo.transaction():
            return self._write(link_code, bucket_ts, observed_latency_ms, observed_reliability)

    def correct(
        self,
        link_code: str,
        bucket_ts: int,
        observed_latency_ms: float | None,
        observed_reliability: float | None,
    ) -> e.TelemetrySample:
        """更正历史观测：生成新版本，旧版本保留并标记失效，供重算使用。"""
        with self.repo.transaction():
            return self._write(link_code, bucket_ts, observed_latency_ms, observed_reliability)

    def _write(
        self,
        link_code: str,
        bucket_ts: int,
        observed_latency_ms: float | None,
        observed_reliability: float | None,
    ) -> e.TelemetrySample:
        if not isinstance(bucket_ts, int):
            raise ValidationError("bucket_ts 必须为整数秒")
        if self.repo.get_link(link_code) is None:
            raise NotFoundError(f"链路 {link_code} 不存在")
        if observed_latency_ms is not None and observed_latency_ms < 0:
            raise ValidationError("观测时延不能为负")
        if observed_reliability is not None and not 0 <= observed_reliability <= 1:
            raise ValidationError("观测可靠性必须位于 [0, 1] 区间")
        aligned = minute_bucket(bucket_ts)
        if aligned != bucket_ts:
            raise ValidationError(
                f"bucket_ts 必须按分钟对齐，建议取值 {aligned}",
                {"aligned_bucket_ts": aligned},
            )
        prior = self.repo.get_telemetry(link_code, bucket_ts)
        # 重复遥测：与当前最新观测完全相同的提交视为重发，幂等忽略，
        # 不产生新版本，也不可能影响后续结算与账本。
        if (
            prior is not None
            and prior.observed_latency_ms == observed_latency_ms
            and prior.observed_reliability == observed_reliability
        ):
            return prior
        sample = e.TelemetrySample(
            link_code=link_code,
            bucket_ts=bucket_ts,
            observed_latency_ms=observed_latency_ms,
            observed_reliability=observed_reliability,
            observed=True,
            version=(prior.version + 1) if prior else 1,
        )
        self.repo.upsert_telemetry(sample)
        return sample

    def list_current(self, link_code: str) -> list[e.TelemetrySample]:
        if self.repo.get_link(link_code) is None:
            raise NotFoundError(f"链路 {link_code} 不存在")
        return self.repo.list_telemetry(link_code)

    def list_versions(self, link_code: str, bucket_ts: int) -> list[e.TelemetrySample]:
        aligned = minute_bucket(bucket_ts)
        return self.repo.list_telemetry_versions(link_code, aligned)

    @staticmethod
    def bucket_span(bucket_ts: int) -> tuple[int, int]:
        return bucket_ts, bucket_ts + BUCKET_SECONDS
