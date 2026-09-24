"""应用装配：把端口实现与各应用服务接成一个可运行的平台。"""

from __future__ import annotations

from ..adapters.memory import InMemoryRepository
from ..adapters.time import ControlledClock, SequentialIds
from ..application.admission import AdmissionService
from ..application.catalog import CatalogService
from ..application.operations import OperationsService
from ..application.settlement import SettlementService
from ..application.telemetry import TelemetryService


class Platform:
    """持有全部服务与共享端口的组合根。"""

    def __init__(self, start_time: int | None = None) -> None:
        self.repo = InMemoryRepository()
        self.ids = SequentialIds()
        self.clock = ControlledClock(start_time)
        self.catalog = CatalogService(self.repo, self.ids, self.clock)
        self.admission = AdmissionService(self.repo, self.ids, self.clock)
        self.telemetry = TelemetryService(self.repo, self.clock)
        self.operations = OperationsService(self.repo, self.ids, self.clock)
        self.settlement = SettlementService(self.repo, self.ids, self.clock)
