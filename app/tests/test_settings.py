"""Environment settings and the settings saved from Evaluate my data."""
from __future__ import annotations

import pytest

from product.settings import clear_saved, config_from_env, load_saved, save_saved
from src.config import Config

ENV = ("JUMP_MODE", "JUMP_BYTES", "JUMP_RATIO", "JUMP_WINDOW_MINUTES", "CONFIRM_MIN_RISE", "ALERT_SHARE",
       "GAP_MINUTES", "DAY_OFFSET_HOURS", "LEAK_WINDOW_MINUTES", "LEAK_BYTES", "GROUP_MINUTES", "CARRY_OVER_MAX",
       "PROMOTION_MIN_SAVING_MISSES")


@pytest.fixture()
def clean_env(monkeypatch, tmp_path):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SETTINGS_PATH", str(tmp_path / "settings.json"))
    return monkeypatch


def test_defaults_match_the_library_config(clean_env):
    cfg, base = config_from_env(budget_k=7), Config()
    assert cfg.budget_k == 7
    assert cfg.memory_jump_bytes == base.memory_jump_bytes
    assert cfg.alert_share == base.alert_share
    assert cfg.confirm_min_rise is None
    assert cfg.leak_window_minutes == base.leak_window_minutes
    assert cfg.promotion_min_saving_misses == base.promotion_min_saving_misses == 1.0


def test_env_overrides_and_blank_values(clean_env):
    clean_env.setenv("JUMP_MODE", "relative")
    clean_env.setenv("JUMP_RATIO", "0.4")
    clean_env.setenv("CONFIRM_MIN_RISE", "6e8")
    clean_env.setenv("ALERT_SHARE", "")  # blank means default
    clean_env.setenv("LEAK_WINDOW_MINUTES", "0")  # 0 turns the leak rule off
    cfg = config_from_env()
    assert cfg.memory_jump_mode == "relative"
    assert cfg.memory_jump_ratio == 0.4
    assert cfg.confirm_min_rise == 6e8
    assert cfg.alert_share == Config().alert_share
    assert cfg.leak_window_minutes is None


def test_a_bad_number_is_an_error_not_a_silent_default(clean_env):
    clean_env.setenv("GAP_MINUTES", "ten")
    with pytest.raises(ValueError):
        config_from_env()


def test_saved_settings_round_trip(clean_env):
    assert not load_saved()
    saved = save_saved({"kind": "jump", "z": 6.0, "window_points": 48, "min_points": 2, "direction": "up"})
    assert load_saved()["z"] == saved["z"] == 6.0
    clear_saved()
    assert not load_saved()


def test_promotion_margin_from_env_is_never_negative(clean_env):
    clean_env.setenv("PROMOTION_MIN_SAVING_MISSES", "2.5")
    assert config_from_env().promotion_min_saving_misses == 2.5
    clean_env.setenv("PROMOTION_MIN_SAVING_MISSES", "-1")
    assert config_from_env().promotion_min_saving_misses == 0.0


def test_the_package_version_matches_pyproject():
    """One version string: the API (FastAPI `version`), model cards outside a git
    checkout and pyproject.toml must not drift apart at a release."""
    import re
    from pathlib import Path

    from product import __version__
    from product.app import app

    pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"', pyproject, re.M)
    assert match and match.group(1) == __version__ == app.version
