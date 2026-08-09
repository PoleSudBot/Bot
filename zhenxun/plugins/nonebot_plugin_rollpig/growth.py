from __future__ import annotations

from .config import get_growth_max_expert_level


def expert_level_from_copies(copies: int) -> int:
    return min(max(int(copies or 0) - 1, 0), get_growth_max_expert_level())
