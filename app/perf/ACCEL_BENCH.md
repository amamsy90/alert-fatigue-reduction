# Accelerator benchmark

Numbers below were produced by `app/perf/bench_accel.py` on the machine in the environment line.
Re-run the script to replace them. Do not type GPU timings in by hand.
A cell left `N/A` was not run.

- Environment: Python 3.12.3, Linux-6.12.94+-x86_64-with-glibc2.39
- Alerts in the synthetic sample: 1383
- Resolved auto backend: `sklearn`
- CUDA version: none
- Libraries: scikit-learn 1.9.1, xgboost None, cuml None
- GPU column: no CUDA device and no usable cuML

| Workload | CPU sklearn (s) | GPU (s) | Speedup (CPU/GPU) |
|---|---:|---:|---:|
| Single fit, median of 3 | 0.0413 | N/A | N/A |
| Walk-forward refits (3 prefixes), median of 3 | 0.0897 | N/A | N/A |

## When the GPU does not help

On this data a single logistic fit is often dominated by the CPU feature transformer (scaling, one-hot, TF-IDF), which the GPU path does not replace. Device startup can make one fit slower than sklearn. The walk-forward column is the fairer comparison, and it can still show no gain.

This run: no GPU timing was taken, because this machine has no usable CUDA backend. That is not evidence the GPU would be faster. Re-run on a machine with cuML or XGBoost-on-CUDA to fill the GPU column; a speedup below 1× means the GPU did not help.

The full GAIA walk-forward (`run_walkforward.py`) also spends time on jump detection. That cost is not in the table. A GPU head does not change it.
