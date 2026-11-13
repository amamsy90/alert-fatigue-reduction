"""Alerts-only experiments from milestones M1–M4 (deliverables D1 and D2).

They score log alerts only and never count a metric jump as a catch, so they
are not the system result (FINAL_REPORT.md §A.6). Run from app/, for example:

    python -m experiments.legacy_alerts_only.run_m1 --sample

They still write app/results/m1_*.csv, m2_*.csv, m4_*.csv, stretch_*.csv and
memory_clf_*.csv.
"""
