"""可控时钟与标识生成器，供确定性测试使用。"""

from __future__ import annotations

import itertools
import time


class ControlledClock:
    def __init__(self, start: int | None = None) -> None:
        self._now = start if start is not None else int(time.time())

    def now(self) -> int:
        return self._now

    def advance(self, seconds: int) -> int:
        self._now += seconds
        return self._now

    def set(self, ts: int) -> None:
        self._now = ts


class SystemClock:
    def now(self) -> int:
        return int(time.time())


class SequentialIds:
    """前缀 + 进程内自增序号，生成可读且确定的标识。"""

    def __init__(self) -> None:
        self._counters: dict[str, itertools.count] = {}

    def next_code(self, prefix: str) -> str:
        counter = self._counters.setdefault(prefix, itertools.count(1))
        return f"{prefix}-{next(counter):04d}"
