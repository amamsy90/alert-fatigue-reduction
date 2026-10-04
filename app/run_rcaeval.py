"""Same memory fault on three microservice applications (RCAEval RE1).

Needs `python ../tools/fetch_rcaeval_mem.py` first. Online Boutique, Sock Shop
and Train Ticket are scored separately. Each recording is a separate experiment,
so the detector never sees one recording's last sample as the next recording's
previous sample (that boundary is not a real jump). The first half of each
application's recordings tunes the queue; the later half is scored once. The
later recordings are then laid end to end only to apply the daily item budget.
The GAIA threshold is not used.

    python run_rcaeval.py [train_fraction]

Writes results/rcaeval_mem.csv and results/rcaeval_mem_settings.csv.
"""
from __future__ import annotations

import itertools
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd

from src.config import Config, day_of
from src.entities import Fault
from src.generic import (
    GRIDS,
    MIN_INCIDENTS_PER_SERIES,
    DetectorSetting,
    _chance,
    _cost,
    _episodes_from,
    _scores,
)
from src.memory_spikes import Spike
from src.queue import build_queue, oracle_ceiling, score_queue, spike_items, tune_confirm_thresholds
from src.tracking import record_run

ROOT = Path("data/rcaeval")
RAW = ROOT / "_raw"
ORIGIN = pd.Timestamp("2023-01-01", tz="UTC")
BUDGETS = (2, 5, 10)
KINDS = ("jump", "delta", "static")


def _slug(name: str) -> str:
    return name.lower().replace(" ", "-")


def _long(raw: pd.DataFrame) -> pd.DataFrame:
    mem_cols = [c for c in raw.columns if str(c).endswith("_mem")]
    frame = raw[["time", *mem_cols]].melt("time", var_name="series_id", value_name="value")
    frame["timestamp"] = pd.to_datetime(frame["time"], unit="s", utc=True)
    return frame.drop(columns="time")


def _load(system: str) -> list[tuple[pd.DataFrame, Fault]]:
    index = pd.read_parquet(RAW / "cases.parquet")
    mem = index[(index["suite"] == "RE1") & (index["fault"] == "mem") & (index["system_name"] == system)]
    cases = []
    for rec in mem.sort_values("time_start").itertuples(index=False):
        raw = pd.read_parquet(RAW / rec.case / "metrics.parquet")
        frame = _long(raw)
        series = f"{rec.root_cause_service}_mem"
        if series not in set(frame["series_id"]):
            raise SystemExit(f"{rec.case} has no {series}")
        start = pd.to_datetime(int(rec.inject_time), unit="s", utc=True)
        end = pd.to_datetime(int(rec.time_end), unit="s", utc=True)
        cases.append((frame, Fault(str(rec.case), start, end, series, "RCAEval RE1 memory fault")))
    if not cases:
        raise SystemExit(f"no RE1 memory cases for {system}")
    return cases


def _rise_gb(cases: list[tuple[pd.DataFrame, Fault]]) -> float:
    rises = []
    for frame, fault in cases:
        series = frame.loc[frame["series_id"] == fault.component].set_index("timestamp")["value"].sort_index()
        before = series[series.index < fault.start]
        during = series[(series.index >= fault.start) & (series.index <= fault.end)]
        if before.empty or during.empty:
            continue
        baseline = before.iloc[-min(60, len(before)):]
        rises.append(float(during.median() - baseline.median()) / 1e9)
    return float(pd.Series(rises).median()) if rises else float("nan")


def _scores_of(cases: list[tuple[pd.DataFrame, Fault]], setting: DetectorSetting) -> dict:
    """Score each recording alone, then pool the scores. A case boundary is a time gap, not a step."""
    chunks: dict[str, list[pd.Series]] = {}
    gaps: dict[str, pd.Timedelta] = {}
    # Levels, when set, already come from the training recordings. A cut after
    # every sample keeps an unset scale from treating the recording as empty.
    far = pd.Timestamp("2100-01-01", tz="UTC")
    for frame, _ in cases:
        cuts = {str(sid): far for sid in frame["series_id"].unique()}
        for sid, (score, gap) in _scores(frame, setting, cuts).items():
            chunks.setdefault(sid, []).append(score)
            gaps[sid] = gap
    return {sid: (pd.concat(parts).sort_index(), gaps[sid]) for sid, parts in chunks.items()}


def _delta_levels(cases: list[tuple[pd.DataFrame, Fault]]) -> dict[str, tuple[float, float]]:
    diffs: dict[str, list[pd.Series]] = {}
    values: dict[str, list[pd.Series]] = {}
    for frame, _ in cases:
        for sid, part in frame.groupby("series_id"):
            series = part.set_index("timestamp")["value"].sort_index()
            diffs.setdefault(str(sid), []).append(series.diff().abs())
            values.setdefault(str(sid), []).append(series)
    levels = {}
    for sid, parts in diffs.items():
        quiet = pd.concat(parts)
        train = pd.concat(values[sid])
        scale = max(
            float(quiet.quantile(0.75) - quiet.quantile(0.25)) / 1.349,
            float(train.std(ddof=0)) * 0.05,
            1e-9,
        )
        levels[sid] = (0.0, scale)
    return levels


def _static_levels(cases: list[tuple[pd.DataFrame, Fault]], z: float) -> dict[str, tuple[float, float]]:
    values: dict[str, list[pd.Series]] = {}
    for frame, _ in cases:
        for sid, part in frame.groupby("series_id"):
            values.setdefault(str(sid), []).append(part.set_index("timestamp")["value"])
    return {
        sid: (float(pd.concat(parts).quantile(1 - z)), float(pd.concat(parts).quantile(z)))
        for sid, parts in values.items()
    }


def _tune(cases: list[tuple[pd.DataFrame, Fault]], faults: list[Fault], kind: str, config: Config) -> tuple[DetectorSetting, dict]:
    """Same grid as src.generic.tune, but each recording is scored on its own."""
    grid = GRIDS[kind]
    default = DetectorSetting(kind, grid["window_points"][0 if kind != "jump" else 1],
                              {"jump": 6.0, "delta": 6.0, "static": 0.999}[kind], 2, "up")
    if not faults:
        return default, {"tuned": False, "reason": "no incidents in the training recordings"}
    delta_levels = _delta_levels(cases) if kind == "delta" else None
    best = None
    tried = 0
    for window, direction in itertools.product(grid["window_points"], ("up", "both")):
        cached: dict[float, dict] = {}
        for z, mp in itertools.product(grid["z"], grid["min_points"]):
            tried += 1
            setting = DetectorSetting(kind, window, z, mp, direction)
            if kind == "delta":
                setting.levels = delta_levels
            elif kind == "static":
                setting.levels = _static_levels(cases, z)
            # Jump and rate scores ignore z; the static quantile does not.
            cache_key = z if kind == "static" else 0.0
            if cache_key not in cached:
                cached[cache_key] = _scores_of(cases, setting)
            threshold = 0.0 if kind == "static" else z
            spikes = _episodes_from(cached[cache_key], threshold, mp, kind)
            cost, caught, false = _cost(spikes, faults, config)
            key = (cost, -caught, false)
            if best is None or key < best[0]:
                best = (key, setting, caught, false)
    assert best is not None
    (cost, _, _), setting, caught, false = best
    return setting, {"tuned": True, "tried": tried, "train_cost": cost, "train_caught": caught,
                     "train_incidents": len(faults), "train_false": false}


def _jump_sizes(cases, faults, setting: DetectorSetting, config: Config) -> dict[str, float]:
    """Per-series jump size, same rule as src.generic.tune_jump_size_by_series."""
    if setting.kind != "jump" or not faults:
        return {}
    by_series: dict[str, list[Fault]] = {}
    for fault in faults:
        by_series.setdefault(fault.component, []).append(fault)
    base = DetectorSetting("jump", setting.window_points, 0.0, 1, setting.direction)
    scores = _scores_of(cases, base)
    chosen: dict[str, float] = {}
    for sid, own in by_series.items():
        if len(own) < MIN_INCIDENTS_PER_SERIES or sid not in scores:
            continue
        train = {sid: scores[sid]}

        def cost_at(z: float, train=train, own=own) -> tuple:
            cost, caught, false = _cost(_episodes_from(train, z, setting.min_points, "jump"), own, config)
            return (cost, -caught, false)

        best_z, best = setting.z, cost_at(setting.z)
        for z in GRIDS["jump"]["z"]:
            candidate = cost_at(float(z))
            if candidate < best:
                best_z, best = float(z), candidate
        if best < cost_at(setting.z):
            chosen[sid] = best_z
    return chosen


def _detect(cases, setting: DetectorSetting) -> list[tuple[pd.DataFrame, Fault, list[Spike]]]:
    found = []
    for frame, fault in cases:
        threshold = 0.0 if setting.kind == "static" else setting.z
        spikes = _episodes_from(_scores_of([(frame, fault)], setting), threshold, setting.min_points,
                                setting.kind, setting.z_by_series)
        if setting.kind == "jump" and setting.levels:
            kept = []
            for spike in spikes:
                gate = setting.levels.get(str(spike.series_id))
                if gate is None or spike.rise_at_confirm >= float(gate[0]):
                    kept.append(spike)
            spikes = kept
        found.append((frame, fault, spikes))
    return found


def _shift(spike: Spike, delta: pd.Timedelta) -> Spike:
    return Spike(
        spike.series_id, spike.service, spike.start + delta, spike.end + delta,
        peak_rise=spike.peak_rise,
        confirm=None if spike.confirm is None else spike.confirm + delta,
        rise_at_confirm=spike.rise_at_confirm, kind=spike.kind,
        level_at_confirm=spike.level_at_confirm, grace=spike.grace,
    )


def _place(found: list[tuple[pd.DataFrame, Fault, list[Spike]]]) -> tuple[list[Spike], list[Fault], dict, int]:
    """Lay recordings end to end so the daily budget is about monitoring time, not the gaps between experiments."""
    cursor = ORIGIN
    spikes: list[Spike] = []
    faults: list[Fault] = []
    spans: dict[str, list[pd.Timestamp]] = {}
    for frame, fault, own in found:
        delta = cursor - frame["timestamp"].min()
        faults.append(Fault(fault.fault_id, fault.start + delta, fault.end + delta, fault.component, fault.description))
        spikes.extend(_shift(spike, delta) for spike in own)
        for sid, part in frame.groupby("series_id"):
            spans.setdefault(str(sid), []).extend([part["timestamp"].min() + delta, part["timestamp"].max() + delta])
        cursor = cursor + (frame["timestamp"].max() - frame["timestamp"].min()) + pd.Timedelta(seconds=1)
    span = {sid: (min(times), max(times)) for sid, times in spans.items()}
    n_days = max(1, int((day_of(cursor - pd.Timedelta(seconds=1)) - day_of(ORIGIN)).days) + 1)
    return spikes, faults, span, n_days


def _systems() -> list[str]:
    index = pd.read_parquet(RAW / "cases.parquet")
    mem = index[(index["suite"] == "RE1") & (index["fault"] == "mem")]
    return sorted(mem["system_name"].unique())


def main() -> None:
    train_fraction = float(sys.argv[1]) if len(sys.argv) > 1 else 0.5
    if not (RAW / "cases.parquet").exists():
        raise SystemExit("missing data/rcaeval/_raw; run python ../tools/fetch_rcaeval_mem.py")
    config = Config()
    rows: list[dict] = []
    settings: list[dict] = []
    for system in _systems():
        cases = _load(system)
        n_train = max(1, min(len(cases) - 1, int(len(cases) * train_fraction)))
        train, test = cases[:n_train], cases[n_train:]
        train_faults = [fault for _, fault in train]
        fitted, fit = {}, {}
        for kind in KINDS:
            fitted[kind], fit[kind] = _tune(train, train_faults, kind, config)
        if fit["jump"].get("tuned"):
            sizes = _jump_sizes(train, train_faults, fitted["jump"], config)
            if sizes:
                fitted["jump"].z_by_series = sizes
                fit["jump"]["z_by_series"] = len(sizes)
            train_jumps = [spike for _, _, spikes in _detect(train, fitted["jump"]) for spike in spikes]
            global_thr, by_series = tune_confirm_thresholds(train_jumps, train_faults, config)
            if by_series:
                fitted["jump"].levels = {sid: (float(thr),) for sid, thr in by_series.items()}
                fit["jump"]["confirm_series"] = len(by_series)
                fit["jump"]["confirm_global"] = None if global_thr is None else float(global_thr)
        detected = {kind: _detect(test, fitted[kind]) for kind in KINDS}
        chosen = min(KINDS, key=lambda kind: fit[kind]["train_cost"])
        placed = {kind: _place(detected[kind]) for kind in KINDS}
        n_days = placed[chosen][3]
        faults = placed[chosen][1]
        chances = {kind: round(_chance(placed[kind][0], placed[kind][1], placed[kind][2]), 4) for kind in KINDS}
        uncapped = {
            kind: round(_cost(placed[kind][0], placed[kind][1], config)[1] / len(faults), 4) for kind in KINDS
        }
        rise = round(_rise_gb(cases), 2)
        name = _slug(system)
        print(
            f"\n{name}: {frame_series(test)} series, {len(test)} test faults "
            f"({len(train)} to train) over {n_days} day(s); "
            f"median memory rise {rise:.2f} GB; chosen {chosen} "
            f"({fitted[chosen].label()}); jump no-budget {uncapped['jump']}; jump chance {chances['jump']}"
        )
        for k in BUDGETS:
            cfg = replace(config, budget_k=int(k))
            ceiling = oracle_ceiling(faults, cfg)
            for kind, queue in (("static", "static_rule"), ("delta", "rate_rule"), ("jump", "jump_rule")):
                own_spikes, own_faults, _, _ = placed[kind]
                shown = build_queue(spike_items(own_spikes, cfg), cfg)
                row = score_queue(queue, shown, own_faults, n_days, cfg)
                row.update(dataset=name, chance=chances[kind], detection_no_budget=uncapped[kind],
                           oracle_ceiling=round(ceiling, 4), chosen=kind == chosen, median_rise_gb=rise)
                rows.append(row)
            shown = build_queue(spike_items(placed[chosen][0], cfg), cfg)
            row = score_queue("your_queue", shown, faults, n_days, cfg)
            row.update(dataset=name, chance=chances[chosen], detection_no_budget=uncapped[chosen],
                       oracle_ceiling=round(ceiling, 4), chosen=True, median_rise_gb=rise)
            rows.append(row)
            if k == 5:
                for item in rows[-4:]:
                    print(
                        f"  K=5 {item['queue']:12s} caught {item['faults_caught']:2d}/{item['faults']:2d} "
                        f"({item['detection_rate']:.0%})  no-budget {item['detection_no_budget']:.0%}  "
                        f"false/day {item['false_items_per_day']:.2f}"
                    )
        for kind in KINDS:
            settings.append({
                "dataset": name, "kind": kind, "chosen": kind == chosen,
                "label": fitted[kind].label(), "train_cost": fit[kind].get("train_cost"),
                "train_caught": fit[kind].get("train_caught"), "train_false": fit[kind].get("train_false"),
                "train_incidents": len(train_faults), "test_no_budget": uncapped[kind],
                "test_spikes": len(placed[kind][0]), "chance": chances[kind], "median_rise_gb": rise,
                "confirm_series": fit[kind].get("confirm_series", 0),
                "series_sizes": fit[kind].get("z_by_series", 0),
            })
    out = Path("results")
    frame = pd.DataFrame(rows)
    frame.to_csv(out / "rcaeval_mem.csv", index=False)
    pd.DataFrame(settings).to_csv(out / "rcaeval_mem_settings.csv", index=False)
    k5 = frame[frame["budget_k"] == 5].pivot(index="dataset", columns="queue", values="detection_rate")
    print("\nK=5 detection by application:\n" + k5.round(3).to_string())
    mine = frame[(frame["budget_k"] == 5) & (frame["queue"] == "your_queue")]
    record_run(
        "rcaeval_mem",
        config={"budgets": BUDGETS, "train_fraction": train_fraction, "fault": "mem", "suite": "RE1",
                "split": "first half of recordings tune, later recordings score, each recording scored alone"},
        data=[RAW / "cases.parquet"],
        outputs=[out / "rcaeval_mem.csv", out / "rcaeval_mem_settings.csv"],
        metrics={f"K5.{dataset}": float(rate) for dataset, rate in zip(mine["dataset"], mine["detection_rate"])},
    )
    print(f"\nWrote {out / 'rcaeval_mem.csv'}")


def frame_series(cases: list[tuple[pd.DataFrame, Fault]]) -> int:
    return len(set(cases[0][0]["series_id"]))


if __name__ == "__main__":
    main()
