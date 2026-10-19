"""Time alert-classifier training on CPU, and on GPU when a device is visible.

One fit, and a short walk-forward of three refits on growing prefixes. This is
not ``run_walkforward.py``: that script also detects jumps on GAIA. Here only
``AlertClassifier.fit`` is timed, which is the part a GPU head can change.

The text and category transformer stays on CPU. On daily-sized frames a GPU
logistic fit can be no faster, or slower, than sklearn. The table records
that outcome; it does not assume a speedup.

    python perf/bench_accel.py
    python perf/bench_accel.py --out perf/ACCEL_BENCH.md

GPU columns are ``N/A`` when neither cuML nor XGBoost-on-CUDA can run.
The default queue stays sklearn (``auto`` does not select a GPU). This
script still times an explicit GPU backend when one is usable. CPU always runs.
"""
from __future__ import annotations

import argparse
import platform
import sys
import tempfile
import time
from pathlib import Path

# `python perf/bench_accel.py` puts perf/ on sys.path, not app/.
_APP_ROOT = Path(__file__).resolve().parents[1]
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

import numpy as np  # noqa: E402

from src.accel import available_backends  # noqa: E402
from src.classifier import AlertClassifier  # noqa: E402
from src.config import Config  # noqa: E402
from src.ingestion import align_alerts_to_faults, load_alerts, load_faults  # noqa: E402
from src.sample_data import generate  # noqa: E402
from src.tracking import accelerator_snapshot  # noqa: E402

REPEATS = 3
WALK_FRACTIONS = (0.45, 0.70, 0.90)


def _alerts():
    with tempfile.TemporaryDirectory() as tmp:
        generate(tmp, seed=42, days=10)
        alerts = load_alerts(Path(tmp) / "alerts.csv")
        faults = load_faults(Path(tmp) / "faults.csv")
    alerts, _faults = align_alerts_to_faults(alerts, faults)
    alerts.sort(key=lambda alert: alert.timestamp)
    return alerts


def _time_fit(alerts, config: Config, backend: str) -> float:
    started = time.perf_counter()
    AlertClassifier(config, backend=backend).fit(alerts)
    return time.perf_counter() - started


def _time_walk(alerts, config: Config, backend: str) -> float:
    started = time.perf_counter()
    n = len(alerts)
    for fraction in WALK_FRACTIONS:
        cut = max(8, int(n * fraction))
        AlertClassifier(config, backend=backend).fit(alerts[:cut])
    return time.perf_counter() - started


def _median(samples: list[float]) -> float:
    return float(np.median(np.asarray(samples, dtype=np.float64)))


def _measure(alerts, config: Config, backend: str) -> dict[str, float]:
    _time_fit(alerts, config, backend)  # warmup, not recorded
    fits = [_time_fit(alerts, config, backend) for _ in range(REPEATS)]
    walks = [_time_walk(alerts, config, backend) for _ in range(REPEATS)]
    return {"fit_s": _median(fits), "walk_s": _median(walks)}


def _cell(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:.4f}"


def _speedup(cpu: float, gpu: float | None) -> str:
    if gpu is None or gpu <= 0:
        return "N/A"
    return f"{cpu / gpu:.2f}×"


def render(cpu: dict[str, float], gpu: dict[str, float] | None, gpu_backend: str | None, alerts: int) -> str:
    snap = accelerator_snapshot()
    fit_gpu = None if gpu is None else gpu["fit_s"]
    walk_gpu = None if gpu is None else gpu["walk_s"]
    reason = "no CUDA device and no usable cuML" if gpu is None else f"backend `{gpu_backend}`"
    if gpu is None:
        helped = (
            "no GPU timing was taken, because this machine has no usable CUDA backend. "
            "That is not evidence the GPU would be faster. Re-run on a machine with cuML "
            "or XGBoost-on-CUDA to fill the GPU column; a speedup below 1× means the GPU did not help"
        )
    elif fit_gpu is not None and fit_gpu >= cpu["fit_s"]:
        helped = "the GPU fit was slower or no faster than sklearn, so the GPU did not help on this run"
    else:
        helped = "the GPU fit was faster than sklearn on this run"
    return "\n".join(
        [
            "# Accelerator benchmark",
            "",
            "Numbers below were produced by `app/perf/bench_accel.py` on the machine in the environment line.",
            "Re-run the script to replace them. Do not type GPU timings in by hand.",
            "A cell left `N/A` was not run.",
            "",
            f"- Environment: Python {platform.python_version()}, {platform.platform()}",
            f"- Alerts in the synthetic sample: {alerts}",
            f"- Resolved auto backend: `{snap['backend']}`",
            f"- CUDA version: {snap['cuda'] if snap['cuda'] else 'none'}",
            f"- Libraries: scikit-learn {snap['libraries'].get('scikit-learn')}, "
            f"xgboost {snap['libraries'].get('xgboost')}, cuml {snap['libraries'].get('cuml')}",
            f"- GPU column: {reason}",
            "",
            "| Workload | CPU sklearn (s) | GPU (s) | Speedup (CPU/GPU) |",
            "|---|---:|---:|---:|",
            f"| Single fit, median of {REPEATS} | {_cell(cpu['fit_s'])} | {_cell(fit_gpu)} | {_speedup(cpu['fit_s'], fit_gpu)} |",
            f"| Walk-forward refits ({len(WALK_FRACTIONS)} prefixes), median of {REPEATS} | "
            f"{_cell(cpu['walk_s'])} | {_cell(walk_gpu)} | {_speedup(cpu['walk_s'], walk_gpu)} |",
            "",
            "## When the GPU does not help",
            "",
            "On this data a single logistic fit is often dominated by the CPU feature transformer "
            "(scaling, one-hot, TF-IDF), which the GPU path does not replace. Device startup can "
            "make one fit slower than sklearn. The walk-forward column is the fairer comparison, "
            "and it can still show no gain.",
            "",
            f"This run: {helped}.",
            "",
            "The full GAIA walk-forward (`run_walkforward.py`) also spends time on jump detection. "
            "That cost is not in the table. A GPU head does not change it.",
            "",
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Time CPU and optional GPU classifier training.")
    parser.add_argument("--out", type=Path, help="Write the markdown report to this path")
    args = parser.parse_args()
    alerts = _alerts()
    config = Config(random_seed=42)
    cpu = _measure(alerts, config, "sklearn")
    usable = available_backends()
    gpu_backend = None
    gpu = None
    # The default queue is sklearn. Time a GPU head only as a measurement:
    # logistic cuML when it can run, otherwise the explicit tree backend.
    if usable["cuml"]:
        gpu_backend = "cuml"
    elif usable["xgboost-gpu"]:
        gpu_backend = "xgboost-gpu"
    if gpu_backend is not None:
        gpu = _measure(alerts, config, gpu_backend)
    report = render(cpu, gpu, gpu_backend, len(alerts))
    print(report)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8")
        print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
