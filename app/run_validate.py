"""Check that saved result files still match the published headline numbers."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

ROOT = Path("results")


def _fail(msg: str) -> None:
    raise SystemExit(f"VALIDATION FAIL: {msg}")


def _near(got: float, expected: float, tol: float, name: str) -> None:
    if abs(got - expected) > tol:
        _fail(f"{name}={got} expected {expected} ± {tol}")


def main() -> None:
    queue = pd.read_csv(ROOT / "queue_eval_gaia.csv")
    k20 = queue[(queue["queue"] == "shared_jumps_1st") & (queue["budget_k"] == 20)].iloc[0]
    _near(float(k20["detection_rate"]), 0.7284, 0.005, "K=20 jumps-first detection")
    base = queue[(queue["queue"] == "baseline") & (queue["budget_k"] == 20)].iloc[0]
    _near(float(base["detection_rate"]), 0.0, 0.001, "K=20 baseline detection")

    jumps = pd.read_csv(ROOT / "memory_gaia_jump.csv")
    _near(float(jumps.iloc[0]["detection_rate"]), 0.861, 0.01, "jump overlap")

    walk = pd.read_csv(ROOT / "walkforward_gaia.csv")
    rates = walk.loc[walk["queue"] == "shared_jumps_1st", "detection_rate"]
    _near(float(rates.mean()), 0.747, 0.02, "walk-forward mean detection")
    if float(rates.min()) < 0.4:
        _fail("walk-forward min detection unexpectedly collapsed")

    stats = pd.read_csv(ROOT / "stats_gaia.csv").iloc[0]
    _near(float(stats["mean"]), 0.747, 0.02, "stats walk-forward mean")
    if int(stats["wins_vs_baseline"]) != 8:
        _fail("queue must beat baseline in all 8 folds")
    if str(stats["injection_tags"]) != "memory_anomalies:1367":
        _fail("every injected fault must still be a memory anomaly")
    silent = int(stats["signal_silent"])
    if silent < 1:
        _fail("expected some test faults with neither a log nor a jump")

    print("VALIDATION OK")
    print(f"  K=20 jumps-first  {float(k20['detection_rate']):.1%}")
    print(f"  jump overlap      {float(jumps.iloc[0]['detection_rate']):.1%}")
    print(f"  walk-forward mean {float(rates.mean()):.1%}  (min {float(rates.min()):.1%})")
    print(
        f"  95% CI            {float(stats['ci_low']):.1%}-{float(stats['ci_high']):.1%}  "
        f"wins {int(stats['wins_vs_baseline'])}/8"
    )


if __name__ == "__main__":
    main()
