"""Queue settings from environment variables, so a team can tune it without code.

| Variable            | Default    | Meaning |
|---------------------|------------|---------|
| JUMP_MODE           | absolute   | `absolute` (rise in metric units) or `relative` (rise / recent median) |
| JUMP_BYTES          | 0.5e9      | absolute mode: rise that makes a point hot (0.5 GB for container memory) |
| JUMP_RATIO          | 0.3        | relative mode: 0.3 = +30% over the 15-minute median |
| JUMP_WINDOW_MINUTES | 15         | length of the "recent median" window |
| CONFIRM_MIN_RISE    | (none)     | only show jumps whose rise at confirmation is at least this |
| ALERT_SHARE         | 0.25       | share of the daily budget log alerts may use |
| GAP_MINUTES         | 10         | no metric point for this long = a monitoring-gap item |
| DAY_OFFSET_HOURS    | 0          | hours added to UTC before cutting on-call days |
"""
from __future__ import annotations

import os

from src.config import Config


def _float(name: str, default: float | None) -> float | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def config_from_env(budget_k: int = 20) -> Config:
    base = Config()
    return Config(
        budget_k=budget_k,
        memory_jump_mode=os.environ.get("JUMP_MODE", base.memory_jump_mode),
        memory_jump_bytes=_float("JUMP_BYTES", base.memory_jump_bytes),
        memory_jump_ratio=_float("JUMP_RATIO", base.memory_jump_ratio),
        memory_jump_window_minutes=int(_float("JUMP_WINDOW_MINUTES", base.memory_jump_window_minutes)),
        confirm_min_rise=_float("CONFIRM_MIN_RISE", None),
        alert_share=_float("ALERT_SHARE", base.alert_share),
        gap_minutes=int(_float("GAP_MINUTES", base.gap_minutes)),
        day_offset_hours=_float("DAY_OFFSET_HOURS", base.day_offset_hours),
    )
