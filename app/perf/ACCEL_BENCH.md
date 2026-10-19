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

## Measured on an RTX 5090 Laptop

Run on ai-ROG, 19 Oct 2026. Ubuntu 26.04.1, driver 595.99.02, `nvidia-smi` CUDA 13.2, RTX 5090 Laptop GPU 24 GB. Python 3.12.15, scikit-learn 1.9.1, xgboost 3.4.1, cuml-cu13 26.10.0, torch 2.14.1+cu130. `app/perf/bench_accel.py`, median of 3, warm-up excluded. xgboost-gpu rows are after the fit accepts device `cuda:0`. The GAIA rows use the same timer with the alert list swapped for the GAIA alerts (34,863). The script's own heading still said "synthetic sample" for that wrapper; the counts below are the ones that were timed.

| Data | Backend | Single fit CPU (s) | GPU (s) | Speedup | Walk-forward 3 refits CPU (s) | GPU (s) | Speedup |
|---|---|---:|---:|---:|---:|---:|---:|
| synthetic 1383 alerts | cuml | 0.0410 | 0.0648 | 0.63× | 0.0566 | 0.1400 | 0.40× |
| synthetic 1383 alerts | xgboost-gpu | 0.0426 | 0.1179 | 0.36× | 0.0566 | 0.2145 | 0.26× |
| GAIA 34863 alerts | cuml | 1.4522 | 1.2450 | 1.17× | 2.5543 | 2.5713 | 0.99× |
| GAIA 34863 alerts | xgboost-gpu | 1.3704 | 1.4116 | 0.97× | 2.4592 | 3.6586 | 0.67× |

The GPU gives no meaningful training speedup on this data. The only cell above 1× is the GAIA single fit with cuML at 1.17×, and the walk-forward is 0.99×. Fit time is dominated by the CPU feature transformer.

CPU vs explicit cuML queue agreement, same tolerances as `tests/test_accel.py` (mean |ΔP| ≤ 0.05, escalated-set Jaccard ≥ 0.90):

| Data | train/test alerts | mean abs ΔP | max abs ΔP | Jaccard | Result |
|---|---:|---:|---:|---:|---|
| synthetic 14d | 945/921 | 0.010894 | 0.130252 | 1.0000 | pass |
| GAIA alerts (week 1 / rest, no spikes) | 3630/31233 | 0.000410 | 0.146521 | 0.9856 | pass |

Three same-seed cuML fits differed by up to 0.0375 in probability, which is why `auto` stays sklearn. A second cuML comparison gave mean |ΔP| 0.009423 (synthetic) and 0.000498 (GAIA), Jaccard 1.0000 and 0.9874.
