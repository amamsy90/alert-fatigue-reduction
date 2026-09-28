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
| LEAK_WINDOW_MINUTES | 180        | slow-leak detector window; 0 = off |
| LEAK_BYTES          | 0.5e9      | slow-leak rise over the long median |
| GROUP_MINUTES       | 0          | metric items this close on other services share one slot |
| CARRY_OVER_MAX      | 0          | unused slots carried to the next day (e.g. 10) |
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from src.config import Config


def _optional_float(name: str) -> float | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    return float(raw)


def _float(name: str, default: float) -> float:
    value = _optional_float(name)
    return default if value is None else value


def config_from_env(budget_k: int = 20) -> Config:
    base = Config()
    return Config(
        budget_k=budget_k,
        memory_jump_mode=os.environ.get("JUMP_MODE", base.memory_jump_mode),
        memory_jump_bytes=_float("JUMP_BYTES", base.memory_jump_bytes),
        memory_jump_ratio=_float("JUMP_RATIO", base.memory_jump_ratio),
        memory_jump_window_minutes=int(_float("JUMP_WINDOW_MINUTES", base.memory_jump_window_minutes)),
        confirm_min_rise=_optional_float("CONFIRM_MIN_RISE"),
        alert_share=_float("ALERT_SHARE", base.alert_share),
        gap_minutes=int(_float("GAP_MINUTES", base.gap_minutes)),
        day_offset_hours=_float("DAY_OFFSET_HOURS", base.day_offset_hours),
        leak_window_minutes=int(_float("LEAK_WINDOW_MINUTES", base.leak_window_minutes or 0)) or None,
        leak_rise_bytes=_float("LEAK_BYTES", base.leak_rise_bytes),
        group_minutes=_float("GROUP_MINUTES", base.group_minutes),
        carry_over_max=int(_float("CARRY_OVER_MAX", base.carry_over_max)),
    )


# ---------------------------------------------------------------------------
# Settings saved from the "Evaluate my data" page (or `cli tune`). They replace
# the built-in memory-jump rule for the live queue until they are reset.


DEFAULT_SETTINGS_PATH = Path(__file__).resolve().parent / "data" / "settings.json"


def settings_path() -> Path:
    return Path(os.environ.get("SETTINGS_PATH") or DEFAULT_SETTINGS_PATH)


def load_saved() -> dict | None:
    path = settings_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_saved(data: dict) -> dict:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)
    return data


def clear_saved() -> None:
    path = settings_path()
    if path.exists():
        path.unlink()


def live_detector():
    """The saved detector (src.generic.DetectorSetting), or None for the built-in rule."""
    saved = load_saved()
    if not saved or "detector" not in saved:
        return None
    from src.generic import setting_from_dict

    return setting_from_dict(saved)
