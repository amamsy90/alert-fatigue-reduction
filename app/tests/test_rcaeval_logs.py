"""The RCAEval RE2/RE3 runner's input rules (no dataset needed)."""
from __future__ import annotations

import pandas as pd
import pytest

import run_rcaeval_logs as rl
from src.memory_spikes import Spike


def test_level_tokens_decide_and_info_lines_are_not_alerts():
    assert rl.log_severity("2024-01-01 ERROR [auth] token check failed") == "error"
    assert rl.log_severity("WARN  slow response from carts") == "warning"
    assert rl.log_severity("INFO  request error count reset") is None  # the level token wins
    assert rl.log_severity("failed to retrieve ads") == "error"  # no level token: error words
    assert rl.log_severity("GET /cart 200 12ms") is None


def test_mask_merges_lines_that_differ_only_in_ids_and_numbers():
    assert rl.mask("order 1234 for user 9f8e7d6c5b4a failed") == rl.mask("order 77 for user 0123456789ab failed")


def _case() -> rl.Case:
    return rl.Case("re2tt_x_cpu_1", day=3, start=1_000, inject=1_720, end=2_440, root="ts-x", fault="cpu")


def test_each_case_gets_its_own_day_and_the_fault_window_follows_the_injection():
    case = _case()
    fault = case.as_fault()
    assert fault.start == rl.ORIGIN + pd.Timedelta(days=3) + rl.DAY_OFFSET + pd.Timedelta(minutes=12)
    assert fault.end - fault.start == pd.Timedelta(minutes=12)
    assert fault.component == "ts-x"
    placebo = case.placebo()
    assert placebo.end == fault.start  # the quiet part, before the injection
    assert placebo.start == case.at(case.start) + pd.Timedelta(minutes=rl.WARMUP_MINUTES)


def test_jumps_in_the_first_minutes_of_a_recording_are_dropped():
    case = _case()

    def jump(minutes: float) -> Spike:
        at = case.at(case.start) + pd.Timedelta(minutes=minutes)
        return Spike(series_id="ts-x_cpu", service="ts-x", start=at, end=at)

    assert rl.early_in_recording(jump(1))
    assert not rl.early_in_recording(jump(rl.WARMUP_MINUTES + 1))


def test_wilson_interval():
    low, high = rl.wilson(33, 45)
    assert low == pytest.approx(0.590, abs=1e-3) and high == pytest.approx(0.840, abs=1e-3)
    assert rl.wilson(0, 0) == (0.0, 0.0)
    assert rl.wilson(15, 15)[1] == 1.0
