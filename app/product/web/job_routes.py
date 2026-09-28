"""Scoring jobs, the queue, Real / Noise marks, uploads, webhook ingest, and the pages."""
from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, Response

from product.alertmanager import looks_like_alertmanager, normalize_ingest, validate_rows
from product.model_store import activate_version, load_model, load_registry
from product.monitoring import record_ingest
from product.pipeline import JobBusy, run_job
from product.web import context as ctx
from product.web.schemas import FeedbackRequest, RunRequest
from product.web.uploads import _budget, _save_upload

router = APIRouter()


@router.post("/jobs/run")
def jobs_run(body: RunRequest):
    _budget(body.budget_k)
    try:
        if body.mode == "ingest":
            rows, last_id = ctx.STORE.peek_buffer()
            if not rows:
                raise HTTPException(400, "Ingest buffer is empty")
            result = run_job(
                ctx.STORE,
                mode="ingest",
                budget_k=body.budget_k,
                shadow_mode=body.shadow_mode,
                alert_rows=rows,
            )
            ctx.STORE.ack_buffer(last_id)  # only after the job was saved
        else:
            result = run_job(
                ctx.STORE,
                mode=body.mode,
                budget_k=body.budget_k,
                shadow_mode=body.shadow_mode,
            )
        return result
    except HTTPException:
        raise
    except JobBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — surface to UI
        raise HTTPException(500, str(exc)) from exc


@router.get("/queue")
def queue(
    job_id: str | None = None,
    day: str | None = None,
    action: str | None = Query(None, description="escalate|review|suppress|all"),
    limit: int = 500,
):
    job_id = job_id or ctx.STORE.latest_job_id()
    if not job_id:
        raise HTTPException(404, "No jobs yet. Run sample or GAIA first.")
    meta = ctx.STORE.job_meta(job_id)
    if meta is None:
        raise HTTPException(404, f"Unknown job_id {job_id}")
    rows = ctx.STORE.get_queue(job_id, day=day, action=action, limit=limit)
    jumps, jumps_total = ctx.STORE.get_jumps(job_id, day=day, per_day=meta["budget_k"])
    return {
        "job": meta,
        "days": ctx.STORE.list_days(job_id),
        "items": rows,
        "count": len(rows),
        "jumps": jumps,
        "jumps_total": jumps_total,
        "feedback": ctx.STORE.feedback_summary(),
        "model": model_status(),
    }


@router.post("/feedback")
def feedback(body: FeedbackRequest):
    try:
        return ctx.STORE.set_feedback(body.kind, body.id, body.verdict)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/model")
def model_status():
    saved = load_model()
    if saved is None:
        return {"saved": False}
    return {"saved": True, "version": load_registry()["active"], **saved.status(ctx.STORE.feedback_summary()["real"])}


@router.get("/models")
def list_models():
    """Every kept model version with its model card; `active` scores live batches."""
    registry = load_registry()
    return {"active": registry["active"], "versions": registry["versions"], "history": registry.get("history", [])}


@router.post("/models/{version}/activate")
def activate_model(version: str):
    """Roll back (or forward) to a kept version. The file's SHA-256 must still match."""
    if not version.startswith("v") or not version[1:].isdigit():
        raise HTTPException(400, "Bad model version")
    try:
        entry = activate_version(version)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"active": entry["version"], "model": entry}


@router.get("/feedback/faults.csv")
def feedback_faults():
    """Confirmed items as fault windows; upload with the next alerts to retrain."""
    columns = ["fault_id", "start", "end", "component", "description"]
    lines = [",".join(columns)]
    for fault in ctx.STORE.feedback_faults():
        lines.append(",".join(str(fault[c]).replace(",", ";") for c in columns))
    return Response(
        "\n".join(lines) + "\n",
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="faults.csv"'},
    )


@router.post("/jobs/upload")
async def jobs_upload(
    alerts_file: UploadFile | None = File(None),
    metrics_file: UploadFile | None = File(None),
    faults_file: UploadFile | None = File(None),
    budget_k: int = Form(20),
    shadow_mode: bool = Form(True),
):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        alerts_path = await _save_upload(alerts_file, tmp_path / "alerts.csv")
        metrics_path = await _save_upload(metrics_file, tmp_path / "metrics.csv")
        faults_path = await _save_upload(faults_file, tmp_path / "faults.csv")
        try:
            result = await run_in_threadpool(
                run_job,
                ctx.STORE,
                mode="upload",
                budget_k=_budget(budget_k),
                shadow_mode=shadow_mode,
                alerts_csv=alerts_path,
                metrics_path=metrics_path,
                faults_path=faults_path,
            )
        except HTTPException:
            raise
        except JobBusy as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, str(exc)) from exc
    return result


@router.post("/ingest/alerts")
@router.post("/ingest/alertmanager")
async def ingest_alerts(request: Request):
    """Accept our `{alerts: [...]}` body or a raw Alertmanager webhook."""
    try:
        payload = await request.json()
        rows = normalize_ingest(payload)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Invalid JSON: {exc}") from exc
    if not rows:
        return {"buffered_batches": 0, "received": 0, "note": "no firing alerts"}
    received = len(rows)
    rows, rejected = validate_rows(rows)
    if not rows:
        raise HTTPException(400, {"message": "no usable alerts", "rejected": len(rejected), "errors": rejected[:5]})
    score_now = False
    budget_k = 20
    if isinstance(payload, dict) and not looks_like_alertmanager(payload):
        score_now = bool(payload.get("score_now"))
        budget_k = _budget(payload.get("budget_k") or 20)
    if request.query_params.get("score_now") in ("1", "true", "yes"):
        score_now = True
    if request.query_params.get("budget_k"):
        budget_k = _budget(request.query_params["budget_k"])
    buffered, duplicates = ctx.STORE.buffer_new_alerts(rows)
    record_ingest(len(rows) - duplicates, duplicates)
    response: dict = {"buffered_batches": buffered, "received": received, "duplicates": duplicates}
    if rejected:
        response.update(rejected=len(rejected), errors=rejected[:5])
    if score_now:
        rows_now, last_id = ctx.STORE.peek_buffer()
        try:
            result = await run_in_threadpool(
                run_job, ctx.STORE, mode="ingest", budget_k=budget_k, alert_rows=rows_now
            )
        except JobBusy as exc:
            raise HTTPException(409, f"{exc} (alerts kept in the buffer)") from exc
        except Exception as exc:  # noqa: BLE001 — rows stay buffered for the next try
            raise HTTPException(400, f"{exc} (alerts kept in the buffer)") from exc
        ctx.STORE.ack_buffer(last_id)
        response["job"] = result
    return response


@router.get("/", response_class=HTMLResponse)
def index():
    html_path = ctx.STATIC_DIR / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    html_path = ctx.STATIC_DIR / "dashboard.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))
