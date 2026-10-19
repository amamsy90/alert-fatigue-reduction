"""Experiment log: one JSON line per experiment run in results/experiments.jsonl.

Each line records what ran, on which code (git commit, uncommitted changes or
not), with which library versions and configuration, on which data (a
fingerprint of the input files), what it wrote (a SHA-256 per output file),
the headline metrics, and why it was run (EXPERIMENT_NOTE, or a note passed in).
The file is kept in git, so `git log -p results/experiments.jsonl` is the
history of every result.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import platform
import subprocess
from collections.abc import Iterable
from importlib import metadata
from pathlib import Path

import pandas as pd

from .accel import available_backends, cuda_version, get_backend

LOG = Path("results") / "experiments.jsonl"
GAIA_INPUTS = ("data/gaia_raw/run/run", "data/gaia_metrics")
LIBRARIES = ("pandas", "numpy", "scikit-learn")
# Optional. None when the extra is not installed; recorded either way.
ACCEL_LIBRARIES = ("scikit-learn", "xgboost", "cuml", "lightgbm")


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(["git", *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def code_state() -> dict:
    """Commit, and whether the code an experiment runs (src/ and the run_*.py
    scripts) had uncommitted changes. The web app in product/ is not part of it."""
    status = _git("status", "--porcelain", "--untracked-files=no", "--", "src", "run_*.py")
    return {"commit": _git("rev-parse", "--short", "HEAD"), "dirty": bool(status) if status is not None else None}


def fingerprint(paths: Iterable[Path | str]) -> str:
    """Cheap, stable fingerprint of input data: relative path and size of every file.

    Contents are not hashed (GAIA is large); a changed, added or removed file
    changes the fingerprint.
    """
    digest = hashlib.sha256()
    for root in sorted(Path(p) for p in paths):
        files = sorted(f for f in root.rglob("*") if f.is_file()) if root.is_dir() else [root]
        for f in files:
            if f.exists():
                rel = f.relative_to(root.parent) if root.is_dir() else f.name
                digest.update(f"{rel}|{f.stat().st_size}\n".encode())
    return digest.hexdigest()[:16]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _plain(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: _plain(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, float):
        return round(value, 6)
    return value


def _version(dist_name: str) -> str | None:
    try:
        return metadata.version(dist_name)
    except metadata.PackageNotFoundError:
        return None


def accelerator_snapshot() -> dict:
    """Backend, library versions and CUDA version for one experiment or model card.

    ``backend`` is what ``get_backend()`` would train with on this machine.
    The default (``auto``) is sklearn even when cuML can run. ``ACCEL_BACKEND``
    selects ``cuml`` or ``xgboost-gpu`` only when that name is set.
    ``libraries`` includes the optional GPU packages even when they are absent.
    """
    versions: dict[str, str | None] = {name: _version(name) for name in ACCEL_LIBRARIES}
    if versions.get("cuml") is None:
        for dist_name in ("cuml-cu12", "cuml-cu13", "cuml-cu11"):
            found = _version(dist_name)
            if found is not None:
                versions["cuml"] = found
                break
    return {
        "backend": get_backend(),
        "cuda": cuda_version(),
        "libraries": versions,
        "available": available_backends(),
    }


def ml_snapshot() -> dict:
    """Torch, safetensors and XGBoost versions, plus the optional-model flags.

    Versions are ``None`` when the package is not installed. The flags default
    to off. This does not probe a GPU beyond the CUDA version string.
    """
    return {
        "torch": _version("torch"),
        "safetensors": _version("safetensors"),
        "xgboost": _version("xgboost"),
        "cuda": cuda_version(),
        "metric_model": os.environ.get("METRIC_MODEL", "").strip().lower() or "off",
        "learned_ranker": os.environ.get("LEARNED_RANKER", "").strip().lower() or "off",
        "trace_model": os.environ.get("TRACE_MODEL", "").strip().lower() or "off",
        "llm_lora": os.environ.get("LLM_LORA", "").strip().lower() or "off",
        "log_encoder": os.environ.get("LOG_ENCODER", "").strip().lower() or "off",
    }


def record_run(
    experiment: str,
    *,
    config=None,
    data: Iterable[Path | str] = (),
    outputs: Iterable[Path | str] = (),
    metrics: dict | None = None,
    note: str | None = None,
    log: Path = LOG,
) -> dict:
    """Append one entry to the experiment log and return it."""
    versions = {lib: _version(lib) for lib in LIBRARIES}
    entry = {
        "at": pd.Timestamp.now("UTC").isoformat(timespec="seconds"),
        "experiment": experiment,
        "note": note or os.environ.get("EXPERIMENT_NOTE") or None,
        "code": code_state(),
        "python": platform.python_version(),
        "libraries": versions,
        "accelerator": accelerator_snapshot(),
        "config": _plain(config) if config is not None else None,
        "data": fingerprint(data) if data else None,
        "outputs": {Path(p).name: _sha256(Path(p)) for p in outputs if Path(p).exists()},
        "metrics": _plain(metrics or {}),
        "ml": ml_snapshot(),
    }
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def read_log(log: Path = LOG) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
