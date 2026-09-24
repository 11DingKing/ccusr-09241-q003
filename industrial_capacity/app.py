"""组合根:装配存储、端口与应用服务,供 HTTP 层与测试复用。"""

from __future__ import annotations

from .adapters.clock import SystemClock
from .adapters.idgen import UuidGenerator
from .adapters.memory import InMemoryStore
from .application.ports import Clock, IdGenerator
from .application.services import (
    AdmissionService,
    AppConfig,
    CatalogService,
    RemediationService,
    SettlementService,
    TelemetryService,
)


class App:
    """平台应用:持有全部应用服务与运行配置。"""

    def __init__(
        self,
        *,
        store: InMemoryStore | None = None,
        clock: Clock | None = None,
        id_gen: IdGenerator | None = None,
        config: AppConfig | None = None,
    ) -> None:
        self.store = store or InMemoryStore()
        self.clock = clock or SystemClock()
        self.id_gen = id_gen or UuidGenerator()
        self.config = config or AppConfig()

        self.catalog = CatalogService(self.store, self.clock, self.id_gen)
        self.admission = AdmissionService(self.store, self.clock, self.id_gen)
        self.remediation = RemediationService(self.store, self.clock, self.id_gen)
        self.settlement = SettlementService(self.store, self.clock, self.id_gen, self.config)
        self.telemetry = TelemetryService(self.store, self.clock, self.id_gen, self.config)

        # 服务间协作:维护窗口触发处置;遥测驱动处置与结算调和
        self.catalog.set_remediation(self.remediation)
        self.telemetry.wire(self.remediation, self.settlement)


def create_app(**kwargs) -> App:
    return App(**kwargs)
