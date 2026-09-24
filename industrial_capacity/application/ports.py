"""可替换端口：时钟、标识生成。"""

from __future__ import annotations

from typing import Protocol


class Clock(Protocol):
    """时间源：全部使用整数秒的 Unix 时间，便于确定性复现。"""

    def now(self) -> int:
        ...


class IdGenerator(Protocol):
    """标识生成端口。"""

    def next_code(self, prefix: str) -> str:
        ...


class EventRecorder(Protocol):
    """领域事件外发端口（本地实现仅收集，不做副作用）。"""

    def record(self, event: dict) -> None:
        ...
