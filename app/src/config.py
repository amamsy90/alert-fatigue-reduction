"""Configuration parameters (design spec section 5)."""
from dataclasses import dataclass, field

import pandas as pd


@dataclass
class Config:
    # Daily human review budget: max items surfaced per day
    budget_k: int = 20
    # Cost of a missed fault vs. a false alarm
    miss_cost: float = 10.0
    false_alarm_cost: float = 1.0
    # Baseline: severities that interrupt a person (GAIA run logs use
    # standard log levels: error / warning / info)
    escalate_severities: tuple[str, ...] = ("critical", "error", "high")
    # Severity ranking used when the budget forces a choice (higher = more urgent)
    severity_rank: dict[str, int] = field(
        default_factory=lambda: {
            "critical": 4,
            "error": 4,
            "high": 3,
            "medium": 2,
            "warning": 2,
            "low": 1,
            "info": 0,
        }
    )
    # Budget sweep used by the evaluation harness
    budget_sweep: tuple[int, ...] = (5, 10, 20, 50)
    # Time-based split: train on week 1, test on week 2 (day index boundary)
    split_day: int = 7
    # Old spike rule (comparison only): value > week-1 mean + k * std.
    memory_spike_k: float = 3.0
    memory_min_points: int = 2
    memory_gap_minutes: int = 2
    # Memory jumps: a point is hot when it rises this far above its own recent median.
    memory_jump_bytes: float = 0.5e9
    memory_jump_window_minutes: int = 15
    # "absolute": rise > memory_jump_bytes. "relative": rise > memory_jump_ratio * median
    # (use relative for metrics that are not container memory in bytes).
    memory_jump_mode: str = "absolute"
    memory_jump_ratio: float = 0.3
    # "robust" mode: hot when the deviation from the recent median is more than
    # robust_z robust standard deviations (IQR / 1.349). Unit-free, any metric.
    robust_z: float = 5.0
    # robust mode only: "up" (rises), "down" (drops) or "both"
    jump_direction: str = "up"
    # Causal queue: a confirmed jump is shown only if its rise at confirmation is at
    # least this big. None = use every jump; tuned on the training window by
    # src.queue.tune_confirm_threshold. Series in `confirm_min_rise_by_series`
    # use their own frozen value instead; everyone else uses this global one.
    confirm_min_rise: float | None = None
    confirm_min_rise_by_series: dict[str, float] = field(default_factory=dict)
    # GAIA experiments: use the per-series gates for "proposed" too. Off by default so
    # the published tables use the single week-1 gate; the per-series variant is its
    # own row (p3_per_series). The web app learns per-series gates regardless.
    confirm_per_series: bool = False
    # Share of the daily budget log alerts may use (jumps and gaps are never blocked
    # by alerts). 0.25 of K=20 is 5 alert slots.
    alert_share: float = 0.25
    # Share of the daily budget monitoring-gap items may use (a flapping feed must
    # not push out detections). 0.25 of K=20 is 5 gap slots.
    gap_share: float = 0.25
    # A series with no point for this long is a monitoring gap (one queue item).
    gap_minutes: int = 10
    # Gaps starting within this many minutes on different services are one item.
    gap_group_minutes: int = 10
    # Phase 3 options, off by default (see run_queue_eval.py rows p3_*):
    # metric items within this many minutes on other services share one slot
    group_minutes: float = 0.0
    # unused slots carried to the next day, at most this many
    carry_over_max: int = 0
    # Slow-leak detector: a second jump rule with a 3-hour median window, so a leak
    # that ramps over hours is still caught (run_robustness.py). None = off.
    leak_window_minutes: int | None = 180
    leak_rise_bytes: float = 0.5e9
    leak_min_points: int = 20
    # Hours added to UTC before cutting days (e.g. 8 for a China-time on-call day).
    day_offset_hours: float = 0.0
    # Promotion gate margin, in missed faults: a newly trained model must make the
    # held-out queue cheaper than both alternatives (no log alerts, and what runs if
    # it is refused) by at least this many x miss_cost. One missed fault is the
    # smallest step a single held-out case can move the queue cost, so a smaller
    # saving could come from one case alone (RCAEval RE2 Online Boutique: 3 false
    # items fewer in 45 days, docs/report_appendices_full.md section B5). 0 = any strict saving
    # (the rule up to v1.0.2). Env: PROMOTION_MIN_SAVING_MISSES.
    promotion_min_saving_misses: float = 1.0
    # Reproducibility
    random_seed: int = 42


def day_of(ts: pd.Timestamp, config: "Config | None" = None) -> pd.Timestamp:
    """On-call day of `ts`: UTC shifted by `day_offset_hours`, then cut at midnight.

    Every per-day budget (queue, alert policy, severity baseline, stored `day`)
    uses this, so all of them agree on where a day starts.
    """
    offset = config.day_offset_hours if config is not None else 0.0
    return (ts + pd.Timedelta(hours=offset)).normalize()
