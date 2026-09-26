"""Per-series confirm gates are learned on the training window and then frozen."""
import numpy as np
import pandas as pd

from product.pipeline import score_alerts
from src.config import Config
from src.entities import Fault
from src.memory_spikes import Spike
from src.queue import spike_items, tune_confirm_thresholds

T0 = pd.Timestamp("2026-01-05", tz="UTC")
SPLIT = T0 + pd.Timedelta(days=7)


def _jump(minute, service, rise):
    start = T0 + pd.Timedelta(minutes=minute)
    return Spike(
        service, service, start, start + pd.Timedelta(minutes=5), rise,
        confirm=start + pd.Timedelta(seconds=30), rise_at_confirm=rise,
    )


def _fault(minute, service, fid):
    start = T0 + pd.Timedelta(minutes=minute)
    return Fault(fid, start, start + pd.Timedelta(minutes=10), service)


def _pair(service, real_rise, noise_rise, minute0):
    """One real jump and four smaller noise jumps, all before the split."""
    real = _jump(minute0, service, real_rise)
    noise = [_jump(minute0 + 40 * (i + 1), service, noise_rise) for i in range(4)]
    fault = _fault(minute0, service, f"f-{service}")
    return [real, *noise], [fault]


def test_two_series_learn_different_thresholds():
    small_j, small_f = _pair("small", 0.9e9, 0.40e9, 10)
    big_j, big_f = _pair("big", 3.0e9, 1.20e9, 15)
    _, by_series = tune_confirm_thresholds(small_j + big_j, small_f + big_f, Config())
    assert set(by_series) == {"small", "big"}
    assert by_series["small"] < by_series["big"]
    assert by_series["small"] <= 0.9e9
    assert by_series["big"] > 1.20e9


def test_a_short_series_keeps_the_global_threshold():
    small_j, small_f = _pair("small", 0.9e9, 0.40e9, 10)
    lonely = _jump(20, "thin", 2.0e9)
    lonely_fault = _fault(20, "thin", "f-thin")
    global_thr, by_series = tune_confirm_thresholds(
        small_j + [lonely], small_f + [lonely_fault], Config(), min_jumps=3
    )
    assert "thin" not in by_series
    assert global_thr is not None
    # The thin series is shown or hidden by the global gate, not its own number.
    kept = spike_items(
        [lonely],
        Config(confirm_min_rise=global_thr, confirm_min_rise_by_series=by_series),
    )
    assert (len(kept) == 1) == (lonely.rise_at_confirm >= global_thr)


def test_test_window_cannot_move_a_series_threshold():
    train_j, train_f = _pair("alpha", 1.0e9, 0.45e9, 30)
    _, train_only = tune_confirm_thresholds(train_j, train_f, Config(), train_until=SPLIT)
    late_small = _jump(8 * 24 * 60, "alpha", 0.2e9)
    late_huge = _jump(8 * 24 * 60, "alpha", 9.0e9)
    late_fault = _fault(8 * 24 * 60, "alpha", "f-late")
    _, with_small = tune_confirm_thresholds(
        train_j + [late_small], train_f + [late_fault], Config(), train_until=SPLIT
    )
    _, with_huge = tune_confirm_thresholds(
        train_j + [late_huge], train_f + [late_fault], Config(), train_until=SPLIT
    )
    assert train_only["alpha"] == with_small["alpha"] == with_huge["alpha"]


def test_each_series_is_gated_by_its_own_frozen_level():
    cfg = Config(confirm_min_rise=0.5e9, confirm_min_rise_by_series={"strict": 0.8e9, "loose": 0.3e9})
    strict = _jump(10, "strict", 0.6e9)
    loose = _jump(10, "loose", 0.6e9)
    shown = spike_items([strict, loose], cfg)
    assert [item.service for item in shown] == ["loose"]


def _byte_series(service, train_events, late_rise):
    """Flat 2 GB series with a few absolute jumps, plus one jump on day 8."""
    idx = pd.date_range(T0, T0 + pd.Timedelta(days=9), freq="30s", inclusive="left")
    values = np.full(len(idx), 2e9)
    for day, hour, rise in train_events:
        start = T0 + pd.Timedelta(days=day, hours=hour)
        end = start + pd.Timedelta(minutes=6)
        values[(idx >= start) & (idx < end)] = 2e9 + rise
    late = T0 + pd.Timedelta(days=8, hours=4)
    values[(idx >= late) & (idx < late + pd.Timedelta(minutes=6))] = 2e9 + late_rise
    return pd.DataFrame(
        {"timestamp": idx, "value": values, "service": service, "series_id": service}
    )


def _product_batch(small_late, big_late, extra_fault=False):
    small_train = [(1, 2, 0.9e9), (1, 8, 0.55e9), (2, 2, 0.55e9), (2, 10, 0.55e9), (3, 4, 0.55e9)]
    big_train = [(1, 3, 3.0e9), (1, 9, 1.2e9), (2, 3, 1.2e9), (2, 11, 1.2e9), (3, 5, 1.2e9)]
    metrics = pd.concat(
        [
            _byte_series("small", small_train, small_late),
            _byte_series("big", big_train, big_late),
        ],
        ignore_index=True,
    )
    faults = []
    for service, events in (("small", small_train), ("big", big_train)):
        day, hour, _ = events[0]
        start = T0 + pd.Timedelta(days=day, hours=hour)
        faults.append(Fault(f"f-{service}", start, start + pd.Timedelta(minutes=6), service))
    if extra_fault:
        late = T0 + pd.Timedelta(days=8, hours=4)
        faults.append(Fault("f-late", late, late + pd.Timedelta(minutes=6), "small"))
    return metrics, faults


def test_product_score_freezes_a_different_level_per_series():
    """The queue learns on week 1 and applies those levels to week 2."""
    cfg = Config(leak_window_minutes=None)
    first_metrics, faults = _product_batch(0.52e9, 2.0e9)
    first = score_alerts([], metrics=first_metrics, faults=faults, config=cfg, use_saved_detector=False)
    levels = first.confirm_by_series
    assert set(levels) == {"small", "big"}
    assert levels["small"] < levels["big"]
    assert levels["small"] <= 0.9e9
    assert levels["big"] > 1.2e9
    # 0.52 GB is a jump, but it is under small's frozen level. 2 GB clears big's.
    shown = {(j["service"], j["confirm"][:10]) for j in first.jumps if j["kind"] == "jump" and j["shown"]}
    assert ("small", "2026-01-13") not in shown
    assert ("big", "2026-01-13") in shown

    changed_metrics, changed_faults = _product_batch(9.0e9, 0.2e9, extra_fault=True)
    second = score_alerts(
        [], metrics=changed_metrics, faults=changed_faults, config=cfg, use_saved_detector=False
    )
    assert second.confirm_by_series == levels
