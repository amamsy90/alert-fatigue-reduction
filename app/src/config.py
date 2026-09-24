"""Configuration parameters (design spec section 5)."""
from dataclasses import dataclass, field


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
    # Reproducibility
    random_seed: int = 42
