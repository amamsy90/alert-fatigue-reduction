"""Stats helpers stay honest on tiny lists — no GAIA download."""
import pandas as pd

from src.entities import Alert, Fault
from src.eval_stats import (
    bootstrap_mean_ci,
    by_service_rows,
    exclusive_window_days,
    jumps_starting_in,
    sign_test_greater,
    signal_counts,
)
from src.memory_spikes import Spike


def test_bootstrap_interval_covers_the_mean():
    interval = bootstrap_mean_ci([0.5, 0.6, 0.7], n_draw=2000)
    assert interval.n == 3
    assert 0.59 < interval.mean < 0.61
    assert interval.low <= interval.mean <= interval.high


def test_sign_test_all_wins_is_one_in_two_to_the_n():
    test = sign_test_greater([0.7, 0.8, 0.6], [0.0, 0.0, 0.0])
    assert test.wins == 3
    assert test.n == 3
    assert test.p_value == 0.125


def test_sign_test_five_of_eight_is_not_the_all_win_p():
    left = [0.8] * 5 + [0.0] * 3
    right = [0.0] * 5 + [0.4] * 3
    test = sign_test_greater(left, right)
    assert test.wins == 5
    assert test.n == 8
    assert abs(test.p_value - 93 / 256) < 1e-9


def test_exclusive_window_days_counts_the_last_partial_day():
    start = pd.Timestamp("2021-08-26", tz="UTC")
    midnight = pd.Timestamp("2021-08-31", tz="UTC")
    last_plus = pd.Timestamp("2021-08-31 15:00:01", tz="UTC")
    assert exclusive_window_days(start, midnight) == 5
    assert exclusive_window_days(start, last_plus) == 6


def test_jumps_starting_in_uses_the_fold_window():
    start = pd.Timestamp("2021-08-19", tz="UTC")
    jumps = [
        Spike("s", "dbservice1", start, start + pd.Timedelta(minutes=5), 1e9),
        Spike("s", "dbservice1", start + pd.Timedelta(days=7), start + pd.Timedelta(days=7, minutes=5), 1e9),
    ]
    assert jumps_starting_in(jumps, start, start + pd.Timedelta(days=7)) == 1


def test_silent_fault_has_neither_log_nor_jump():
    start = pd.Timestamp("2021-07-10", tz="UTC")
    end = start + pd.Timedelta(minutes=10)
    faults = [
        Fault("f1", start, end, "dbservice1", "memory_anomalies"),
        Fault("f2", start, end, "webservice1", "memory_anomalies"),
    ]
    alerts = [
        Alert(
            "a1",
            start + pd.Timedelta(minutes=1),
            "dbservice1",
            "warning",
            "warning",
            "mem",
            "f1",
        )
    ]
    jumps = [Spike("s", "dbservice1", start, end, 1e9)]
    counts = signal_counts(faults, alerts, jumps)
    assert counts["log_and_jump"] == 1
    assert counts["silent"] == 1
    rows = by_service_rows(faults, alerts, jumps)
    by_name = {row["service"]: row for row in rows}
    assert by_name["dbservice1"]["with_jump"] == 1
    assert by_name["webservice1"]["with_jump"] == 0
