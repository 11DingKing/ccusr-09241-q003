"""应用端口:时钟与标识生成均可替换,便于稳定复现业务过程。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """返回当前 UTC 时刻。"""
        ...


class IdGenerator(Protocol):
    def new_id(self, prefix: str) -> str:
        """生成带前缀的唯一标识,如 ``rsv_01J...``。"""
        ...
