"""标识生成适配:生产用 UUID,测试用确定性序列。"""

from __future__ import annotations

import itertools
import uuid


class UuidGenerator:
    def new_id(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:16]}"


class SequentialGenerator:
    """确定性 ID:同一批测试数据每次运行产生相同标识。"""

    def __init__(self) -> None:
        self._counter = itertools.count(1)

    def new_id(self, prefix: str) -> str:
        return f"{prefix}_{next(self._counter):06d}"
