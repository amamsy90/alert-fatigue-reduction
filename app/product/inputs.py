"""Load one batch for a scoring job: alerts, metrics and (when known) faults.

Each mode reads its own source. Only `ingest` may pull live metrics from
Prometheus, and only then is "silent until now" real (JobInputs.live_metrics).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import pandas as pd

from src.config import Config
from src.entities import Alert, Fault
from src.gaia_loader import load_run_folder
from src.ingestion import align_alerts_to_faults, load_alerts, load_faults
from src.memory_spikes import load_metric_folder
from src.sample_data import generate_demo_day

from .settings import config_from_env

APP_ROOT = Path(__file__).resolve().parent.parent


def _default_paths() -> dict[str, Path]:
    return {
        "gaia_run": APP_ROOT / "data" / "gaia_raw" / "run" / "run",
        "gaia_metrics": APP_ROOT / "data" / "gaia_metrics",
        "sample_dir": APP_ROOT / "data" / "sample_product",
    }


def load_alerts_from_dicts(rows: list[dict]) -> list[Alert]:
    alerts: list[Alert] = []
    for i, row in enumerate(rows):
        ts = pd.to_datetime(row["timestamp"], utc=True)
        alerts.append(
            Alert(
                alert_id=str(row.get("alert_id") or f"w{i:06d}"),
                timestamp=ts,
                source=str(row["source"]),
                alert_type=str(row.get("alert_type") or row.get("severity") or "info"),
                severity=str(row.get("severity") or "info").lower(),
                message=str(row.get("message") or "")[:300],
                fault_id=row.get("fault_id"),
            )
        )
    alerts.sort(key=lambda a: a.timestamp)
    return alerts


def load_metrics_csv(path: str | Path) -> pd.DataFrame:
    """Accept either a folder of GAIA-style files or one CSV with timestamp,value,service."""
    path = Path(path)
    if path.is_dir():
        return load_metric_folder(path)
    # Any column names: timestamp/time/date, value/count/..., series/service/host/...;
    # long or wide (one column per series). See src/generic.py.
    from src.generic import load_metrics

    frame, _ = load_metrics(path, default_series=path.stem)
    return frame


@dataclass
class JobInputs:
    alerts: list[Alert]
    metrics: pd.DataFrame | None = None
    faults: list[Fault] | None = None
    source: str = "sample"
    # Only the sample overrides the environment's settings (its series is bytes).
    config: Config | None = None
    # Pulled from Prometheus just now: a series silent until now is a dead feed.
    live_metrics: bool = False


def load_inputs(
    mode: str,
    *,
    budget_k: int = 20,
    alerts_csv: str | Path | None = None,
    metrics_path: str | Path | None = None,
    alert_rows: list[dict] | None = None,
    faults_path: str | Path | None = None,
) -> JobInputs:
    """Read the batch for `mode` (sample, gaia, upload, ingest)."""
    paths = _default_paths()
    if mode == "sample":
        generate_demo_day(paths["sample_dir"])
        alerts = load_alerts(paths["sample_dir"] / "alerts.csv")
        faults = load_faults(paths["sample_dir"] / "faults.csv")
        alerts, faults = align_alerts_to_faults(alerts, faults)
        # Keep the leak window from the environment (0 turns leaks off) but
        # score this byte series with the absolute 0.5 GB rule it was built for.
        config = replace(
            config_from_env(budget_k),
            memory_jump_mode="absolute",
            memory_jump_bytes=Config().memory_jump_bytes,
        )
        return JobInputs(alerts, load_metrics_csv(paths["sample_dir"] / "metrics.csv"), faults, "sample", config)
    if mode == "gaia":
        run_path = paths["gaia_run"]
        if not run_path.exists():
            raise FileNotFoundError(f"GAIA run folder not found: {run_path}")
        alerts, faults = load_run_folder(run_path)
        metrics_dir = paths["gaia_metrics"]
        metrics = load_metric_folder(metrics_dir) if metrics_dir.exists() else None
        return JobInputs(alerts, metrics, faults, "gaia")
    if mode == "upload":
        from src.generic import load_events, load_incidents

        if alerts_csv is None and alert_rows is None and not metrics_path:
            raise ValueError("upload needs an alerts/events file, a metrics file, or both")
        metrics = load_metrics_csv(metrics_path) if metrics_path else None
        known = set(metrics["series_id"].astype(str)) if metrics is not None else None
        if alert_rows is not None:
            alerts = load_alerts_from_dicts(alert_rows)
        elif alerts_csv is not None:
            alerts, _ = load_events(alerts_csv, known)
        else:
            alerts = []
        incidents: list[Fault] | None = None
        if faults_path:
            incidents, _ = load_incidents(faults_path, known_series=known)
            alerts, incidents = align_alerts_to_faults(alerts, incidents)
        return JobInputs(alerts, metrics, incidents, "upload")
    if mode == "ingest":
        if not alert_rows:
            raise ValueError("ingest mode needs alert_rows from the buffer")
        alerts = load_alerts_from_dicts(alert_rows)
        if metrics_path:
            return JobInputs(alerts, load_metrics_csv(metrics_path), None, "ingest")
        from .ops import get_prom_url
        from .prometheus_adapter import fetch_memory_metrics

        if get_prom_url():
            return JobInputs(alerts, fetch_memory_metrics(), None, "ingest", live_metrics=True)
        return JobInputs(alerts, None, None, "ingest")
    raise ValueError(f"Unknown mode: {mode}")
