"""Champion and challenger: compare a refused model with the active one on live alerts.

A newly trained model that fails the promotion gate is kept as a candidate. This
module lets that candidate (the challenger) score the same live alerts as the
active model (the champion) in shadow, job after job, and keeps a running tally,
so the decision to promote rests on weeks of live data and not one held-out window.

Cost is the one the gate uses: on the labelled alerts, a fault alert the model
does not escalate is a miss (miss_cost) and a non-fault alert it escalates is a
false item (false_alarm_cost). Only labelled alerts count; an unmarked alert is
unknown, not noise. The tally recommends; a person promotes.

Nothing here changes what is shown or paged. The challenger's decisions are used
only to fill in these counts.
"""
from __future__ import annotations

from collections.abc import Sequence

from .entities import SURFACED_ACTIONS, Action, Alert, Decision

MIN_JOBS = 3           # jobs a challenger must be tallied on before it can be recommended
MIN_LABELLED = 30      # labelled alerts across those jobs
COUNTS = ("alerts", "labelled", "champion_only", "challenger_only", "agree",
          "champion_fp", "champion_fn", "challenger_fp", "challenger_fn")


def _escalated(decision: Decision) -> bool:
    return decision.action == Action.ESCALATE


def compare(
    champion: Sequence[Decision],
    challenger: Sequence[Decision],
    alerts: Sequence[Alert],
    labelled: Sequence[bool],
    *,
    miss_cost: float,
    false_alarm_cost: float,
) -> dict:
    """One job's counts. `labelled[i]` says whether alert i has a known label (fault or noise)."""
    if not (len(champion) == len(challenger) == len(alerts) == len(labelled)):
        raise ValueError("champion, challenger, alerts and labelled must be the same length")
    out: dict[str, float] = dict.fromkeys(COUNTS, 0)
    out["alerts"] = len(alerts)
    for alert, champ, chall, known in zip(alerts, champion, challenger, labelled):
        champ_shown, chall_shown = champ.action in SURFACED_ACTIONS, chall.action in SURFACED_ACTIONS
        if champ_shown == chall_shown:
            out["agree"] += 1
        elif champ_shown:
            out["champion_only"] += 1
        else:
            out["challenger_only"] += 1
        if not known:
            continue
        out["labelled"] += 1
        is_fault = alert.fault_id is not None
        for prefix, decision in (("champion", champ), ("challenger", chall)):
            if is_fault and not _escalated(decision):
                out[f"{prefix}_fn"] += 1
            elif not is_fault and _escalated(decision):
                out[f"{prefix}_fp"] += 1
    out["champion_cost"] = miss_cost * out["champion_fn"] + false_alarm_cost * out["champion_fp"]
    out["challenger_cost"] = miss_cost * out["challenger_fn"] + false_alarm_cost * out["challenger_fp"]
    return out


def accumulate(tally: dict | None, job: dict) -> dict:
    """Add one job's counts to the running tally and update the per-job win count."""
    tally = dict(tally or {})
    for key in (*COUNTS, "champion_cost", "challenger_cost"):
        tally[key] = tally.get(key, 0) + job.get(key, 0)
    tally["jobs"] = tally.get("jobs", 0) + 1
    if job.get("labelled", 0):
        tally["judged_jobs"] = tally.get("judged_jobs", 0) + 1
        if job["challenger_cost"] < job["champion_cost"]:
            tally["challenger_wins"] = tally.get("challenger_wins", 0) + 1
        elif job["challenger_cost"] > job["champion_cost"]:
            tally["champion_wins"] = tally.get("champion_wins", 0) + 1
    for key in ("judged_jobs", "challenger_wins", "champion_wins"):
        tally.setdefault(key, 0)
    return tally


def recommend(tally: dict, margin: float) -> tuple[str, str]:
    """collecting, promote or keep, with the reason.

    promote needs all of: enough jobs and labelled alerts, a total saving of at
    least `margin` (the same margin the promotion gate uses, so one lucky alert
    cannot decide it), and more jobs won than lost. The recommendation never
    promotes by itself.
    """
    jobs, labelled = tally.get("judged_jobs", 0), tally.get("labelled", 0)
    if jobs < MIN_JOBS or labelled < MIN_LABELLED:
        return "collecting", (f"{jobs} of {MIN_JOBS} jobs and {labelled} of {MIN_LABELLED} labelled alerts "
                              "tallied so far")
    saving = tally["champion_cost"] - tally["challenger_cost"]
    wins, losses = tally.get("challenger_wins", 0), tally.get("champion_wins", 0)
    detail = f"saves {saving:g} (margin {margin:g}) over {jobs} jobs; won {wins}, lost {losses}"
    if saving >= margin and wins > losses:
        return "promote", detail
    return "keep", detail
