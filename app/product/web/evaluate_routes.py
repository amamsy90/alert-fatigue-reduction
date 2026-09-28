"""Evaluate my data: upload or pick a dataset, tune and score, apply the settings."""
from __future__ import annotations

import json
import os
import shutil
import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse

from product import datasets as example_data
from product.pipeline import JobBusy, run_job
from product.settings import clear_saved, load_saved, save_saved
from product.web import context as ctx
from product.web.uploads import _budget, _save_upload

router = APIRouter()


# ---------------------------------------------------------------------------
# Evaluate my data: upload -> tune + evaluate -> use the settings -> score

EVAL_DIR = ctx.EVAL_DIR
_EVAL_LOCK = threading.Lock()


def _eval_folder(eval_id: str) -> Path:
    if not eval_id.isalnum():
        raise HTTPException(400, "Bad evaluation id")
    folder = EVAL_DIR / eval_id
    if not folder.exists():
        raise HTTPException(404, f"Unknown evaluation {eval_id}")
    return folder


@router.get("/evaluate", response_class=HTMLResponse)
def evaluate_page():
    return HTMLResponse((ctx.STATIC_DIR / "evaluate.html").read_text(encoding="utf-8"))


@router.get("/datasets")
def list_example_datasets():
    return {"datasets": example_data.list_datasets()}


@router.post("/datasets/fetch")
def fetch_example_datasets(body: dict | None = None):
    """Download NAB categories (needs internet)."""
    wanted = (body or {}).get("categories") or None
    try:
        return {"downloaded": example_data.fetch_nab(wanted), "datasets": example_data.list_datasets()}
    except KeyError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — offline, proxy, ...
        raise HTTPException(502, f"Could not download NAB: {type(exc).__name__}: {exc}") from exc


@router.post("/evaluate")
async def evaluate_data(
    metrics_file: UploadFile | None = File(None),
    incidents_file: UploadFile | None = File(None),
    events_file: UploadFile | None = File(None),
    dataset: str | None = Form(None),
    budgets: str = Form("5,10,20"),
    train_fraction: float = Form(0.3),
    incident_minutes: float = Form(10.0),
    day_offset_hours: float = Form(0.0),
):
    """Tune on the first `train_fraction` of each series and score the rest.

    Upload your own files, or pick a ready-made `dataset` (see GET /datasets).
    """
    from src.generic import evaluate, live_setting

    # One evaluation at a time: each is CPU-heavy. A separate lock from the
    # scoring job, so a long evaluation never delays a live paging job.
    if not _EVAL_LOCK.acquire(blocking=False):
        raise HTTPException(409, "Another evaluation is running; retry when it finishes")
    eval_id = uuid.uuid4().hex[:12]
    folder = EVAL_DIR / eval_id
    folder.mkdir(parents=True, exist_ok=True)
    try:
        if dataset:
            try:
                files = example_data.dataset_files(dataset)
            except KeyError as exc:
                raise HTTPException(400, str(exc)) from exc
            for role, src in files.items():
                shutil.copyfile(src, folder / f"{role}.csv")
        else:
            if metrics_file is None or not metrics_file.filename:
                raise HTTPException(400, "Upload a metrics file (or choose an example dataset)")
            for role, upload in (("metrics", metrics_file), ("incidents", incidents_file), ("events", events_file)):
                await _save_upload(upload, folder / f"{role}.csv")
        candidates = {role: folder / f"{role}.csv" for role in ("metrics", "incidents", "events")}
        paths = {role: p if p.exists() else None for role, p in candidates.items()}
        try:
            k_list = tuple(_budget(x) for x in budgets.replace(" ", "").split(",") if x)
        except HTTPException as exc:
            raise HTTPException(400, f"budgets must be whole numbers like 5,10,20 ({exc.detail})") from exc
        try:
            report = await run_in_threadpool(
                evaluate, paths["metrics"], paths["incidents"], paths["events"], budgets=k_list or (5, 10, 20),
                train_fraction=min(max(train_fraction, 0.05), 0.9),
                default_incident_minutes=incident_minutes, day_offset_hours=day_offset_hours)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        data = report.to_dict()
        suggested = live_setting(paths["metrics"], report)
        (folder / "report.json").write_text(json.dumps({"report": data, "suggested": suggested,
                                                        "dataset": dataset}, default=str), encoding="utf-8")
        _prune_evals(keep=eval_id)
        return {"eval_id": eval_id, "dataset": dataset, "report": data, "suggested": suggested}
    except BaseException:  # any failure: drop the half-written folder
        shutil.rmtree(folder, ignore_errors=True)
        raise
    finally:
        _EVAL_LOCK.release()


def _prune_evals(keep: str, limit: int | None = None) -> None:
    """Keep the newest EVAL_RETENTION (default 30) evaluation folders; uploads add up."""
    if limit is None:
        try:
            limit = int(os.environ.get("EVAL_RETENTION", "30"))
        except ValueError:
            limit = 30
    if limit <= 0 or not EVAL_DIR.exists():
        return
    folders = sorted((f for f in EVAL_DIR.iterdir() if f.is_dir()), key=lambda f: f.stat().st_mtime, reverse=True)
    for folder in folders[limit:]:
        if folder.name != keep:
            shutil.rmtree(folder, ignore_errors=True)


@router.get("/settings")
def get_settings():
    saved = load_saved()
    if not saved:
        return {"saved": False, "live": "built-in memory-jump rule (JUMP_* environment variables)"}
    return {"saved": True, **saved}


@router.post("/settings")
def apply_settings(body: dict):
    """Use the settings an evaluation suggested for the live queue."""
    eval_id = str(body.get("eval_id") or "")
    stored = json.loads((_eval_folder(eval_id) / "report.json").read_text(encoding="utf-8"))
    saved = save_saved({**stored["suggested"], "eval_id": eval_id, "applied_at": pd_now()})
    return {"saved": True, **saved}


@router.delete("/settings")
def reset_settings():
    clear_saved()
    return {"saved": False, "live": "built-in memory-jump rule"}


@router.post("/evaluate/{eval_id}/score")
def score_evaluated(eval_id: str, body: dict | None = None):
    """Run the live queue on the evaluated files (every day, all items), for the Queue page."""
    folder = _eval_folder(eval_id)
    budget_k = _budget((body or {}).get("budget_k") or 20)
    events = folder / "events.csv"
    incidents = folder / "incidents.csv"
    try:
        return run_job(
            ctx.STORE,
            mode="upload",
            budget_k=budget_k,
            alerts_csv=events if events.exists() else None,
            metrics_path=folder / "metrics.csv",
            faults_path=incidents if incidents.exists() else None,
        )
    except JobBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def pd_now() -> str:
    import pandas as pd

    return pd.Timestamp.now("UTC").isoformat()
