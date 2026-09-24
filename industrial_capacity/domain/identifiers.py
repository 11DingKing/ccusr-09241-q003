"""确定性标识:由内容派生的稳定 ID,保证重放/重算产生完全相同的记录。"""

from __future__ import annotations

import hashlib


def stable_id(prefix: str, *parts: object) -> str:
    """按内容生成确定性 ID,例如 ``bw_3f9a1c2d4e``。"""
    digest = hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:16]}"
