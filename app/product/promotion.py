"""The promotion gate: may a newly trained alert model become the active one?

Pure functions over the held-out report a training run produces, so the rule
can be tested (and read) without running a job. See promotion_decision.
"""
from __future__ import annotations

from src.config import Config

from .model_store import held_out_cost


def no_log_alerts_cost(report: dict | None, config: Config) -> float | None:
    """Held-out cost of showing no log alerts at all: every fault-bearing alert is a miss."""
    if not report:
        return None
    return config.miss_cost * report.get("fault_alerts", 0)


def promotion_margin(config: Config) -> float:
    """Queue-cost saving a new model needs over both alternatives (cost units)."""
    return max(0.0, float(config.promotion_min_saving_misses)) * float(config.miss_cost)


def promotion_decision(training: dict | None, has_incumbent: bool, config: Config) -> tuple[bool, str]:
    """Should a newly trained model become the active one (and score this batch)?

    Three tests on this batch's held-out window, all required:

    1. Alerts: its cost (misses x miss_cost + false alarms) is strictly below
       the cost of showing no log alerts at all.
    2. Queue: the whole queue with its alerts costs less (missed faults x
       miss_cost + false items) than both the queue with no log alerts and the
       queue the batch falls back to if the model is refused (the active
       model's alerts, or rules only = severity), by at least the margin
       `promotion_min_saving_misses` x miss_cost (default one missed fault).
       So the gate tests the model against what would actually run instead,
       and a saving one held-out case could produce on its own (RCAEval RE2
       Online Boutique: 3 false items in 45 days) does not promote it.
       A model can pass 1 and fail 2: on GAIA it finds most fault-bearing
       alerts, but those faults are also caught by metric jumps and its alerts
       take slots the jumps needed (docs/report_appendices_full.md section B4).
    3. If a model is active, its alert cost is no higher than that model's on
       the same alerts.

    The first model is not promoted just because it is first, and without a
    held-out window or a held-out fault nothing changes.
    """
    training = training or {}
    keep = "the active model stays" if has_incumbent else "rules only stays"
    new = held_out_cost(training.get("test"), config.miss_cost, config.false_alarm_cost)
    floor = no_log_alerts_cost(training.get("test"), config)
    if new is None or floor is None:
        return False, f"no held-out window to test on; {keep}"
    if new >= floor:
        return False, (f"held-out cost {new:g} is not below {floor:g} for showing no log alerts; "
                       f"kept as a candidate, {keep}")
    check = training.get("queue_check")
    if not check:
        return False, f"no held-out fault to test the whole queue on; kept as a candidate, {keep}"
    with_model, without = check["cost_with_model"], check["cost_no_log_alerts"]
    fallback = check.get("cost_fallback", without)
    queue = (
        f"queue with its alerts: {check['caught_with_model']}/{check['faults']} held-out faults, "
        f"{check['false_items_with_model']} false items (cost {with_model:g})"
    )
    if "cost_fallback" in check:
        queue += (
            f"; with {check.get('fallback', 'the fallback')} instead: {check['caught_fallback']}/{check['faults']}, "
            f"{check['false_items_fallback']} false items (cost {fallback:g})"
        )
    queue += (
        f"; without log alerts: {check['caught_no_log_alerts']}/{check['faults']}, "
        f"{check['false_items_no_log_alerts']} false items (cost {without:g})"
    )
    if with_model >= without or with_model >= fallback:
        return False, f"{queue}; kept as a candidate, {keep}"
    saving = min(without, fallback) - with_model
    margin = promotion_margin(config)
    if saving < margin:
        return False, (f"{queue}; saves {saving:g}, below the margin of {margin:g} "
                       f"({config.promotion_min_saving_misses:g} missed fault x {config.miss_cost:g}); "
                       f"kept as a candidate, {keep}")
    queue += f"; saves {saving:g} (margin {margin:g})"
    if not has_incumbent:
        return True, f"held-out cost {new:g} < {floor:g} for showing no log alerts; {queue}"
    old = held_out_cost(training.get("incumbent_test"), config.miss_cost, config.false_alarm_cost)
    if old is None:
        return False, f"active model not scored on the held-out window; {keep}"
    if new <= old:
        return True, f"held-out cost {new:g} <= active model's {old:g}; {queue}"
    return False, f"held-out cost {new:g} > active model's {old:g}; kept as a candidate, {keep}"
