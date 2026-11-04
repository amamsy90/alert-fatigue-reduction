"""Run the four queue experiments. Flags default off; this process opts in only when they are set.

Homelab uses the day-12 snapshot. GAIA is scored only when ``app/data/gaia_raw`` and
``app/data/gaia_metrics`` exist. A missing dataset writes a reason and no rate.

The keep rule is the paired bootstrap in ``src.eval_stats``: 4000 draws, seed 42,
KEEP only when the 95% interval on the per-fault catch difference has low > 0.
E4 reports time to detection. It does not use the keep rule.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

from homelab.companies import load_companies  # noqa: E402
from homelab.evaluate import (  # noqa: E402
    _alert_model,
    _bounds,
    _frozen_gate,
    _population,
    lab_config,
    load_tables,
)
from src.baseline import baseline_decisions  # noqa: E402
from src.baselines_metric import detect_delta, detect_static  # noqa: E402
from src.classifier import AlertClassifier  # noqa: E402
from src.config import Config  # noqa: E402
from src.enhancements import (  # noqa: E402
    BOOTSTRAP_DRAWS,
    BOOTSTRAP_SEED,
    CALIBRATION_DAYS,
    CALIBRATION_WEIGHT,
    HYBRID_FIXED_M,
    NEIGHBOUR_MINUTES,
    alertmanager_notifications,
    alertmanager_queue,
    caught_from_notes,
    choose_hybrid_m,
    company_calibration_enabled,
    compare_caught,
    delay_report,
    fine_tune_ranker,
    first_catch_minutes,
    fit_ranker,
    fit_topology_ranker,
    hybrid_from_queues,
    hybrid_queue,
    hybrid_queue_enabled,
    note_catch_minutes,
    recalibrate_threshold,
    require_xgboost,
    topology_features_enabled,
)
from src.experiment import load_gaia  # noqa: E402
from src.model_eval import published_scoring_window  # noqa: E402
from src.features import build_feature_frame  # noqa: E402
from src.jump_ranker import JumpRanker  # noqa: E402
from src.memory_spikes import detect_gaps, detect_jumps, detect_leaks  # noqa: E402
from src.policy import bayes_threshold, spike_review_decisions, tune_threshold  # noqa: E402
from src.queue import alert_items, build_queue, n_days_between, spike_items  # noqa: E402
from src.ranker import caught_fault_ids  # noqa: E402

BUDGETS = (5, 10, 20, 50)
PRODUCT_FLAGS = ("LEARNED_RANKER", "METRIC_MODEL", "LLM_EXPLAIN")
EXPERIMENT_FLAGS = ("RANKER_COMPANY_CALIBRATION", "HYBRID_QUEUE", "RANKER_TOPOLOGY_FEATURES")


def _utc(stamp: pd.Timestamp) -> pd.Timestamp:
    value = pd.Timestamp(stamp)
    if value.tzinfo is None:
        return value.tz_localize("UTC")
    return value.tz_convert("UTC")


def _blank(experiment: str, population: str, variant: str, reason: str, **extra) -> dict:
    row = {
        "experiment": experiment,
        "population": population,
        "variant": variant,
        "k": extra.get("k"),
        "m": extra.get("m"),
        "status": "not-scored",
        "reason": reason,
        "decision": None,
        "faults": None,
        "candidate_caught": None,
        "baseline_caught": None,
        "mean": None,
        "low": None,
        "high": None,
        "baseline": extra.get("baseline"),
    }
    row.update(extra)
    return row


def _scored(experiment, population, variant, comparison, **extra) -> dict:
    row = {
        "experiment": experiment,
        "population": population,
        "variant": variant,
        "status": comparison.get("status"),
        "reason": comparison.get("reason", ""),
        "decision": comparison.get("decision"),
        "faults": comparison.get("faults"),
        "candidate_caught": comparison.get("candidate_caught"),
        "baseline_caught": comparison.get("baseline_caught"),
        "mean": comparison.get("mean"),
        "low": comparison.get("low"),
        "high": comparison.get("high"),
    }
    row.update(extra)
    return row


def _delay_row(experiment, population, variant, k, report) -> dict:
    return {
        "experiment": experiment,
        "population": population,
        "variant": variant,
        "k": k,
        "n_caught": report["n_caught"],
        "median_min": report["median_min"],
        "p90_min": report["p90_min"],
        "n_both": report["n_both"],
        "median_diff": report["median_diff"],
        "low": report["low"],
        "high": report["high"],
    }


def _learned_decisions(alerts, jumps, config, model, threshold):
    if model is None or threshold is None or not alerts:
        return baseline_decisions(alerts, config)
    flags = (
        [bool(value) for value in build_feature_frame(alerts, config, jumps)["during_memory_spike"]]
        if jumps
        else [False] * len(alerts)
    )
    return spike_review_decisions(
        alerts, model.predict_proba(alerts, jumps), config, threshold, bayes_threshold(config), flags
    )


def _fit_alert(train_alerts, train_jumps, config):
    if not train_alerts:
        return None, None, "no-alerts"
    try:
        model = AlertClassifier(config).fit(train_alerts, train_jumps)
        threshold = tune_threshold(train_alerts, model.predict_proba(train_alerts, train_jumps), config)
    except ValueError as exc:
        return None, None, str(exc)
    return model, threshold, "trained"


def _candidate_items(jumps, leaks, gaps, alerts, decisions, config):
    return (
        spike_items(jumps, config)
        + spike_items(leaks, config)
        + spike_items(gaps, config)
        + alert_items(alerts, decisions)
    )


def _compare_queue(shown, baseline_ids, faults):
    return compare_caught(caught_fault_ids(shown, faults), baseline_ids, faults)


def _hybrid_ms(k: int) -> list[tuple[str, int]]:
    return [
        ("m10", min(10, k)),
        ("m20", min(20, k)),
        ("m_cap", min(k, 20)),
    ]


def run_split_population(
    name: str,
    metrics: pd.DataFrame,
    alerts: list,
    faults: list,
    config: Config,
    *,
    backend: str,
    device: str,
    apps: dict[str, str],
    do_hybrid: bool,
    do_topology: bool,
    origin: pd.Timestamp | None = None,
    test_end: pd.Timestamp | None = None,
) -> tuple[list[dict], list[dict]]:
    """Day-7 split for E2, E3 and E4. One detection pass, then every K.

    Homelab leaves ``origin`` and ``test_end`` empty and uses the series span.
    GAIA passes the published alert window so metric points outside the alerts
    cannot add faults.
    """
    catch: list[dict] = []
    delays: list[dict] = []
    if origin is None or test_end is None:
        origin, last = _bounds(metrics, alerts, faults)
        if origin is None or last is None or last - origin <= pd.Timedelta(days=config.split_day):
            catch.append(_blank("E2", name, "hybrid", "series is not longer than the first week"))
            return catch, delays
        train_end = _utc(origin) + pd.Timedelta(days=config.split_day)
        test_end = _utc(last) + pd.Timedelta(seconds=1)
    else:
        origin = _utc(origin)
        test_end = _utc(test_end)
        train_end = origin + pd.Timedelta(days=config.split_day)
        if test_end - origin <= pd.Timedelta(days=config.split_day):
            catch.append(_blank("E2", name, "hybrid", "series is not longer than the first week"))
            return catch, delays
    train_alerts = [alert for alert in alerts if alert.timestamp < train_end]
    test_alerts = [alert for alert in alerts if train_end <= alert.timestamp < test_end]
    train_faults = [fault for fault in faults if fault.start < train_end]
    test_faults = [fault for fault in faults if train_end <= fault.start < test_end]
    if not test_faults:
        catch.append(_blank("E2", name, "hybrid", "no faults start in the test window", k=None))
        return catch, delays
    print(f"{name}: detecting", flush=True)
    train_jumps = detect_jumps(metrics, config, detect_until=train_end) if not metrics.empty else []
    test_jumps = detect_jumps(metrics, config, detect_from=train_end, detect_until=test_end) if not metrics.empty else []
    train_gaps = detect_gaps(metrics, config, detect_until=train_end) if not metrics.empty else []
    test_gaps = detect_gaps(metrics, config, detect_from=train_end, detect_until=test_end) if not metrics.empty else []
    train_leaks = detect_leaks(metrics, config, detect_until=train_end) if not metrics.empty else []
    test_leaks = detect_leaks(metrics, config, detect_from=train_end, detect_until=test_end) if not metrics.empty else []
    static = detect_static(metrics, config, fit_until=train_end, detect_from=train_end, detect_until=test_end) if not metrics.empty else []
    delta = detect_delta(metrics, config, detect_from=train_end, detect_until=test_end) if not metrics.empty else []
    model, alert_thr, alert_status = _fit_alert(train_alerts, train_jumps, config)
    if model is None and alert_status not in {"trained", "no-alerts"}:
        catch.append(_blank("E2", name, "hybrid", alert_status))
        catch.append(_blank("E4", name, "queues", alert_status))
        return catch, delays
    from src.queue import tune_confirm_thresholds

    gate, _by_series = tune_confirm_thresholds(train_jumps, train_faults, config, train_until=train_end)
    tuned_gate = None if gate is None else float(gate)
    history = [alert for alert in alerts if alert.timestamp < test_end]
    try:
        jump_ranker = JumpRanker(config).fit(train_jumps + train_leaks, train_alerts, train_faults)
        ranked_metric = jump_ranker.keep(test_jumps + test_leaks + test_gaps, history)
    except ValueError as exc:
        jump_ranker = None
        ranked_metric = None
        catch.append(_blank("E2", name, "p3_ranker", str(exc)))
    notes = alertmanager_notifications(test_alerts, config)
    n_days = None

    for k in BUDGETS:
        cfg = replace(config, budget_k=k, confirm_min_rise=tuned_gate)
        plain = replace(config, budget_k=k)
        train_decisions = _learned_decisions(train_alerts, train_jumps, plain, model, alert_thr)
        test_decisions = _learned_decisions(test_alerts, test_jumps, plain, model, alert_thr)
        train_items = _candidate_items(train_jumps, train_leaks, train_gaps, train_alerts, train_decisions, cfg)
        test_items = _candidate_items(test_jumps, test_leaks, test_gaps, test_alerts, test_decisions, cfg)
        proposed = build_queue(test_items, cfg)
        proposed_ids = caught_fault_ids(proposed, test_faults)
        proposed_delay = first_catch_minutes(proposed, test_faults)
        n_days = n_days_between(train_end, test_end, cfg)
        ranker, rank_status = fit_ranker(
            train_items, train_faults, cfg, None, train_end=train_end, backend=backend, device=device, seed=BOOTSTRAP_SEED
        )
        if backend == "xgboost" and ranker is not None and ranker.backend != "xgboost":
            raise SystemExit(
                "backend xgboost was requested but the fitted ranker is "
                f"{ranker.backend}. Refusing to score a pointwise fallback."
            )
        learned = ranker.select(test_items, None, cfg) if ranker is not None else None
        topo, topo_status = (None, "off")
        if do_topology:
            topo, topo_status = fit_topology_ranker(
                train_items,
                train_faults,
                cfg,
                train_end,
                seed=BOOTSTRAP_SEED,
                apps=apps,
                backend=backend,
                device=device,
                enabled=True,
            )
        if backend == "xgboost" and topo is not None and topo.backend != "xgboost":
            raise SystemExit(
                "backend xgboost was requested but the topology ranker is "
                f"{topo.backend}. Refusing to score a pointwise fallback."
            )
        topology = topo.select(test_items, None, cfg) if topo is not None else None
        p3 = (
            None
            if ranked_metric is None
            else build_queue(
                spike_items(ranked_metric, plain, use_threshold=False) + alert_items(test_alerts, test_decisions),
                plain,
            )
        )
        queues: dict[str, list | None] = {
            "proposed": proposed,
            "learned_ranker": learned,
            "topology": topology,
            "p3_ranker": p3,
            "jumps_tuned": build_queue(spike_items(test_jumps, cfg), cfg),
            "static_p99": build_queue(spike_items(static, plain), plain),
            "delta_5m": build_queue(spike_items(delta, plain), plain),
            "logs_severity": build_queue(
                alert_items(test_alerts, baseline_decisions(test_alerts, plain)), replace(plain, alert_share=1.0)
            ),
        }
        if learned is None:
            catch.append(_blank("E2", name, "learned_ranker", rank_status, k=k, baseline="proposed"))
        if topology is None and do_topology:
            catch.append(_blank("E3", name, "topology", topo_status, k=k, baseline="proposed"))
        learned_ids = caught_fault_ids(learned, test_faults) if learned is not None else set()
        p3_ids = caught_fault_ids(p3, test_faults) if p3 is not None else set()
        if do_hybrid and ranker is not None:
            selected = choose_hybrid_m(
                train_items, train_faults, ranker, cfg, [min(m, k) for _name, m in _hybrid_ms(k)], enabled=True
            )
            for label, m in _hybrid_ms(k):
                shown = hybrid_queue(test_items, ranker, cfg, m, enabled=True)
                queues[f"hybrid_{label}"] = shown
                for baseline_name, baseline_ids in (("proposed", proposed_ids), ("learned_ranker", learned_ids)):
                    comparison = _compare_queue(shown, baseline_ids, test_faults)
                    catch.append(
                        _scored(
                            "E2",
                            name,
                            f"hybrid_{label}",
                            comparison,
                            k=k,
                            m=m,
                            selected_m=selected,
                            selected=selected == m,
                            baseline=baseline_name,
                            ranker_status=rank_status,
                            n_days=n_days,
                        )
                    )
        if do_hybrid and ranked_metric is not None:
            for label, m in _hybrid_ms(k):
                m_cfg = replace(plain, budget_k=m)
                m_decisions = _learned_decisions(test_alerts, test_jumps, m_cfg, model, alert_thr)
                ranked_m = build_queue(
                    spike_items(ranked_metric, m_cfg, use_threshold=False) + alert_items(test_alerts, m_decisions),
                    m_cfg,
                )
                shown = hybrid_from_queues(ranked_m, proposed, plain, m, enabled=True)
                queues[f"hybrid_jump_{label}"] = shown
                for baseline_name, baseline_ids in (("proposed", proposed_ids), ("p3_ranker", p3_ids)):
                    comparison = _compare_queue(shown, baseline_ids, test_faults)
                    catch.append(
                        _scored(
                            "E2",
                            name,
                            f"hybrid_jump_{label}",
                            comparison,
                            k=k,
                            m=m,
                            baseline=baseline_name,
                            n_days=n_days,
                        )
                    )
        if topology is not None:
            for baseline_name, baseline_ids in (
                ("proposed", proposed_ids),
                ("learned_ranker", learned_ids),
            ):
                comparison = _compare_queue(topology, baseline_ids, test_faults)
                catch.append(
                    _scored(
                        "E3",
                        name,
                        "topology",
                        comparison,
                        k=k,
                        baseline=baseline_name,
                        ranker_status=topo_status,
                        n_days=n_days,
                    )
                )
        for queue_name, shown in queues.items():
            if shown is None:
                continue
            report = delay_report(first_catch_minutes(shown, test_faults), proposed_delay)
            delays.append(_delay_row("E4", name, queue_name, k, report))
        for order in ("severity", "first"):
            shown_notes = alertmanager_queue(notes, plain, order)
            comparison = compare_caught(caught_from_notes(shown_notes, test_faults), proposed_ids, test_faults)
            catch.append(_scored("E4", name, f"alertmanager_{order}", comparison, k=k, baseline="proposed", n_days=n_days))
            delays.append(
                _delay_row("E4", name, f"alertmanager_{order}", k, delay_report(note_catch_minutes(shown_notes, test_faults), proposed_delay))
            )
        print(f"{name} K={k} faults={len(test_faults)} proposed={len(proposed_ids)} ranker_status={rank_status}", flush=True)
    return catch, delays


def run_company_calibration(
    alerts: pd.DataFrame,
    metrics: pd.DataFrame,
    faults: pd.DataFrame,
    companies: list[str],
    config: Config,
    *,
    backend: str,
    device: str,
) -> tuple[list[dict], list[dict]]:
    """E1. Source model sees only the other companies before the held-out N-day boundary."""
    catch: list[dict] = []
    delays: list[dict] = []
    if not company_calibration_enabled():
        catch.append(_blank("E1", "leave-one-company-out", "calibration", "RANKER_COMPANY_CALIBRATION is off"))
        return catch, delays
    for held in companies:
        others = [company for company in companies if company != held]
        _frame, held_alerts, held_faults = _population(alerts, metrics, faults, held)
        held_metrics, _a, _f = _population(alerts, metrics, faults, held)
        origin, last = _bounds(held_metrics, held_alerts, held_faults)
        if origin is None or last is None:
            catch.append(_blank("E1", held, "calibration", "no timestamps"))
            continue
        test_end = _utc(last) + pd.Timedelta(seconds=1)
        source_metrics, source_alerts, source_faults = _population(alerts, metrics, faults, None)
        source_alerts = [alert for alert in source_alerts if alert.source.split("/", 1)[0] in others]
        source_faults = [fault for fault in source_faults if fault.component.split("/", 1)[0] in others]
        if not source_metrics.empty:
            source_metrics = source_metrics[source_metrics["service"].astype(str).str.split("/").str[0].isin(others)].copy()
        for n_days in CALIBRATION_DAYS:
            boundary = _utc(origin) + pd.Timedelta(days=n_days)
            print(f"E1 {held} N={n_days}", flush=True)
            src_alerts = [alert for alert in source_alerts if alert.timestamp < boundary]
            src_faults = [fault for fault in source_faults if fault.start < boundary]
            gate = _frozen_gate(source_metrics, src_faults, config, boundary)
            src_jumps = detect_jumps(source_metrics, config, detect_until=boundary) if not source_metrics.empty else []
            model, alert_thr = _alert_model(src_alerts, src_jumps, config)
            src_start = boundary
            stamps = [alert.timestamp for alert in src_alerts]
            if not source_metrics.empty:
                stamps.append(pd.Timestamp(source_metrics["timestamp"].min()))
            if stamps:
                src_start = min(_utc(stamp) for stamp in stamps)
            source_items = _window_items(source_metrics, src_alerts, config, src_start, boundary, gate, model, alert_thr, 20)
            calib_items = _window_items(held_metrics, held_alerts, config, _utc(origin), boundary, gate, model, alert_thr, 20)
            test_items = _window_items(held_metrics, held_alerts, config, boundary, test_end, gate, model, alert_thr, 20)
            calib_faults = [fault for fault in held_faults if fault.start < boundary]
            test_faults = [fault for fault in held_faults if boundary <= fault.start < test_end]
            if not test_faults:
                catch.append(_blank("E1", held, f"N{n_days}", "no faults after the calibration window", n_days=n_days))
                continue
            source, source_status = fit_ranker(
                source_items,
                src_faults,
                replace(config, budget_k=20),
                None,
                train_end=boundary,
                backend=backend,
                device=device,
                seed=BOOTSTRAP_SEED,
            )
            if source is None:
                catch.append(_blank("E1", held, f"N{n_days}", source_status, n_days=n_days))
                continue
            recal, recal_status = recalibrate_threshold(
                source, calib_items, calib_faults, config, boundary, enabled=True
            )
            tuned, tune_status = fine_tune_ranker(
                source_items,
                src_faults,
                calib_items,
                calib_faults,
                config,
                boundary,
                seed=BOOTSTRAP_SEED,
                enabled=True,
            )
            for k in BUDGETS:
                cfg = replace(config, budget_k=k, confirm_min_rise=gate)
                proposed = build_queue(test_items, cfg)
                unadapted = source.select(test_items, None, cfg)
                proposed_ids = caught_fault_ids(proposed, test_faults)
                unadapted_ids = caught_fault_ids(unadapted, test_faults)
                variants = [("unadapted", unadapted, "scored")]
                if recal is not None and recal_status == "retuned":
                    variants.append(("recalibrated", recal.select(test_items, None, cfg), recal_status))
                elif recal_status != "retuned":
                    catch.append(_blank("E1", held, f"N{n_days}_recalibrated", recal_status, k=k, n_days=n_days))
                if tuned is not None and tune_status == "trained":
                    variants.append(("fine_tuned", tuned.select(test_items, None, cfg), tune_status))
                else:
                    catch.append(_blank("E1", held, f"N{n_days}_fine_tuned", tune_status, k=k, n_days=n_days))
                proposed_delay = first_catch_minutes(proposed, test_faults)
                for label, shown, status in variants:
                    caught = caught_fault_ids(shown, test_faults)
                    for baseline_name, baseline_ids in (("unadapted", unadapted_ids), ("proposed", proposed_ids)):
                        if label == "unadapted" and baseline_name == "unadapted":
                            continue
                        comparison = compare_caught(caught, baseline_ids, test_faults)
                        catch.append(
                            _scored(
                                "E1",
                                held,
                                f"N{n_days}_{label}",
                                comparison,
                                k=k,
                                n_days=n_days,
                                baseline=baseline_name,
                                ranker_status=status,
                                calibration_weight=CALIBRATION_WEIGHT,
                            )
                        )
                    delays.append(
                        _delay_row(
                            "E1",
                            held,
                            f"N{n_days}_{label}",
                            k,
                            delay_report(first_catch_minutes(shown, test_faults), proposed_delay),
                        )
                    )
    return catch, delays


_SPIKES: dict[tuple, tuple] = {}


def _spikes(metrics, config, start, end):
    """Jumps, gaps and leaks for one window. The rolling score is computed once per window."""
    key = (id(metrics), str(start), str(end))
    if key not in _SPIKES:
        if metrics is None or metrics.empty:
            _SPIKES[key] = ([], [], [])
        else:
            _SPIKES[key] = (
                detect_jumps(metrics, config, detect_from=start, detect_until=end),
                detect_gaps(metrics, config, detect_from=start, detect_until=end),
                detect_leaks(metrics, config, detect_from=start, detect_until=end),
            )
    return _SPIKES[key]


def _window_items(metrics, alerts, config, start, end, gate, model, threshold, budget: int):
    jumps, gaps, leaks = _spikes(metrics, config, start, end)
    window_alerts = [alert for alert in alerts if start <= _utc(alert.timestamp) < end]
    cfg = replace(config, budget_k=budget, confirm_min_rise=gate)
    decisions = _learned_decisions(window_alerts, jumps, cfg, model, threshold)
    return _candidate_items(jumps, gaps, leaks, window_alerts, decisions, cfg)


def _company_apps(path: Path | None) -> dict[str, str]:
    return {company.id: company.app for company in load_companies(path) if company.enabled}


def run_homelab(collected: Path, faults_path: Path, out: Path, *, backend: str, device: str, companies_file: Path | None) -> None:
    alerts, metrics, faults = load_tables(collected, faults_path)
    ids = [company.id for company in load_companies(companies_file) if company.enabled]
    present = sorted(set(faults["company"].astype(str)) & set(ids)) if ids else sorted(set(faults["company"].astype(str)))
    present = [company for company in present if company != "cluster"]
    apps = _company_apps(companies_file)
    config = lab_config(7)
    catch: list[dict] = []
    delays: list[dict] = []
    do_hybrid = hybrid_queue_enabled()
    do_topology = topology_features_enabled()
    populations = [("pooled", None)] + [(company, company) for company in present]
    for name, company in populations:
        frame, alert_objs, fault_objs = _population(alerts, metrics, faults, company)
        rows, delay_rows = run_split_population(
            name,
            frame,
            alert_objs,
            fault_objs,
            config,
            backend=backend,
            device=device,
            apps=apps,
            do_hybrid=do_hybrid,
            do_topology=do_topology,
        )
        catch.extend(rows)
        delays.extend(delay_rows)
        _write(out, catch, delays, _meta("homelab", collected, backend, device))
    loco_rows, loco_delays = run_company_calibration(
        alerts, metrics, faults, present, config, backend=backend, device=device
    )
    catch.extend(loco_rows)
    delays.extend(loco_delays)
    if do_topology:
        catch.extend(_loco_topology(alerts, metrics, faults, present, config, apps, backend, device))
    _write(out, catch, delays, _meta("homelab", collected, backend, device))


def _loco_topology(alerts, metrics, faults, companies, config, apps, backend, device) -> list[dict]:
    """E3 on the published leave-one-company-out split (after day 7)."""
    rows = []
    for held in companies:
        others = [company for company in companies if company != held]
        held_metrics, held_alerts, held_faults = _population(alerts, metrics, faults, held)
        origin, last = _bounds(held_metrics, held_alerts, held_faults)
        if origin is None or last is None or last - origin <= pd.Timedelta(days=config.split_day):
            rows.append(_blank("E3", held, "topology_loco", "held-out series is not longer than the first week"))
            continue
        train_end = _utc(origin) + pd.Timedelta(days=config.split_day)
        test_end = _utc(last) + pd.Timedelta(seconds=1)
        source_metrics, source_alerts, source_faults = _population(alerts, metrics, faults, None)
        source_alerts = [alert for alert in source_alerts if alert.source.split("/", 1)[0] in others and alert.timestamp < train_end]
        source_faults = [fault for fault in source_faults if fault.component.split("/", 1)[0] in others and fault.start < train_end]
        if not source_metrics.empty:
            source_metrics = source_metrics[source_metrics["service"].astype(str).str.split("/").str[0].isin(others)].copy()
        gate = _frozen_gate(source_metrics, source_faults, config, train_end)
        src_jumps = detect_jumps(source_metrics, config, detect_until=train_end) if not source_metrics.empty else []
        model, alert_thr = _alert_model(source_alerts, src_jumps, config)
        stamps = [alert.timestamp for alert in source_alerts]
        if not source_metrics.empty:
            stamps.append(pd.Timestamp(source_metrics["timestamp"].min()))
        src_start = min(_utc(stamp) for stamp in stamps) if stamps else train_end
        print(f"E3 loco {held}", flush=True)
        source_items = _window_items(source_metrics, source_alerts, config, src_start, train_end, gate, model, alert_thr, 20)
        test_items = _window_items(held_metrics, held_alerts, config, train_end, test_end, gate, model, alert_thr, 20)
        test_faults = [fault for fault in held_faults if train_end <= fault.start < test_end]
        plain, plain_status = fit_ranker(
            source_items, source_faults, replace(config, budget_k=20), None,
            train_end=train_end, backend=backend, device=device, seed=BOOTSTRAP_SEED,
        )
        topo, topo_status = fit_topology_ranker(
            source_items, source_faults, replace(config, budget_k=20), train_end,
            seed=BOOTSTRAP_SEED, apps=apps, backend=backend, device=device, enabled=True,
        )
        if plain is None or topo is None or not test_faults:
            rows.append(_blank("E3", held, "topology_loco", topo_status if topo is None else plain_status))
            continue
        for k in BUDGETS:
            cfg = replace(config, budget_k=k, confirm_min_rise=gate)
            proposed = build_queue(test_items, cfg)
            base = plain.select(test_items, None, cfg)
            shown = topo.select(test_items, None, cfg)
            for baseline_name, baseline in (("unadapted", base), ("proposed", proposed)):
                comparison = _compare_queue(shown, caught_fault_ids(baseline, test_faults), test_faults)
                rows.append(
                    _scored("E3", held, "topology_loco", comparison, k=k, baseline=baseline_name, ranker_status=topo_status)
                )
    return rows


def run_gaia(out: Path, *, backend: str, device: str) -> None:
    raw = Path("data/gaia_raw")
    metric_dir = Path("data/gaia_metrics")
    if not raw.exists() or not metric_dir.exists():
        _write(
            out,
            [_blank("E2", "gaia", "hybrid", "GAIA folders are not in this worktree. Nothing was scored.")],
            [],
            _meta("gaia", None, backend, device),
        )
        raise SystemExit(
            "GAIA folders are not in this worktree (data/gaia_raw and data/gaia_metrics). Nothing was scored."
        )
    data = load_gaia()
    config = Config()
    origin, train_end, stop, frame, test_faults = published_scoring_window(
        data.metrics, data.alerts, data.faults, config
    )
    print(
        f"gaia window origin={origin} train_end={train_end} stop={stop} test_faults={len(test_faults)}",
        flush=True,
    )
    catch, delays = run_split_population(
        "gaia",
        frame,
        data.alerts,
        data.faults,
        config,
        backend=backend,
        device=device,
        apps={},
        do_hybrid=hybrid_queue_enabled(),
        do_topology=topology_features_enabled(),
        origin=origin,
        test_end=stop,
    )
    catch.append(
        _blank(
            "E1",
            "gaia",
            "calibration",
            "Company calibration is the homelab leave-one-company-out experiment. GAIA is one tenant here.",
        )
    )
    _write(out, catch, delays, _meta("gaia", raw, backend, device))


def _meta(dataset: str, source: Path | None, backend: str, device: str) -> dict:
    return {
        "dataset": dataset,
        "source": None if source is None else str(source),
        "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bootstrap_draws": BOOTSTRAP_DRAWS,
        "bootstrap_seed": BOOTSTRAP_SEED,
        "rule": "KEEP iff the 95% paired bootstrap interval on the per-fault catch difference has low > 0",
        "backend": backend,
        "device": device,
        "calibration_days": list(CALIBRATION_DAYS),
        "calibration_weight": CALIBRATION_WEIGHT,
        "hybrid_m": list(HYBRID_FIXED_M),
        "neighbour_minutes": NEIGHBOUR_MINUTES,
        "experiment_flags": {name: os.environ.get(name) for name in EXPERIMENT_FLAGS},
        "product_flags": {name: os.environ.get(name) for name in PRODUCT_FLAGS},
        "graph": "public demo call graphs for the service names in homelab/catalog.py; GAIA names that miss the graph have fan-out 0",
        "e1_window": "labels strictly before held-out origin + N days; score faults that start after that boundary. Alert decisions in that candidate list use K=20; other K values change only the slot cap.",
        "e4": "median and p90 of minutes from fault start to the first catching shown item; CI is on the median difference versus proposed for faults both caught",
    }


def _write(out: Path, catch: list[dict], delays: list[dict], meta: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    catch_frame = pd.DataFrame(catch)
    delay_frame = pd.DataFrame(delays)
    catch_frame.to_csv(out / "summary.csv", index=False)
    delay_frame.to_csv(out / "detection.csv", index=False)
    (out / "summary.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    kept = catch_frame[catch_frame["decision"] == "KEEP"] if "decision" in catch_frame.columns else catch_frame
    print(f"wrote {out} rows={len(catch_frame)} keep={len(kept)}", flush=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run E1-E4. Set the experiment flags or nothing is scored.")
    parser.add_argument("--data", choices=("homelab", "gaia"), required=True)
    parser.add_argument("--collected", type=Path)
    parser.add_argument("--faults", type=Path)
    parser.add_argument("--companies-file", type=Path, default=None)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--backend", default="pointwise")
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args(argv)
    args.out = args.out.resolve()
    missing = [name for name in EXPERIMENT_FLAGS if not os.environ.get(name, "").strip()]
    if missing:
        _write(
            args.out,
            [_blank("flags", args.data, "off", "Experiment flags are unset, so nothing was scored: " + ", ".join(missing))],
            [],
            _meta(args.data, args.collected, args.backend, args.device),
        )
        print("experiment flags are off; nothing was scored", flush=True)
        return
    for name in PRODUCT_FLAGS:
        if os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}:
            raise SystemExit(f"{name} is on. This script does not run with a product model flag set.")
    require_xgboost(args.backend)
    if args.data == "gaia":
        os.chdir(ROOT / "app")
        run_gaia(args.out, backend=args.backend, device=args.device)
        return
    if args.collected is None or args.faults is None:
        raise SystemExit("homelab needs --collected and --faults")
    run_homelab(
        args.collected,
        args.faults,
        args.out,
        backend=args.backend,
        device=args.device,
        companies_file=args.companies_file,
    )


if __name__ == "__main__":
    main()
