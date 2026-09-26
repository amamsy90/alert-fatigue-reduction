"""FastAPI on-call alert queue — Phase 1 demo + Phase 2 connectors."""
from __future__ import annotations

import base64
import binascii
import ipaddress
import os
import secrets
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import json
import shutil
import uuid

from product import datasets as example_data
from product.alertmanager import looks_like_alertmanager, normalize_ingest
from product.settings import clear_saved, load_saved, save_saved
from product.model_store import load_model
from product.pipeline import run_job
from product.prometheus_adapter import probe_prometheus
from product.store import QueueStore

APP_DIR = Path(__file__).resolve().parent
STORE = QueueStore(os.environ.get("QUEUE_DB") or APP_DIR / "data" / "queue.db")

app = FastAPI(
    title="On-call alert queue",
    description="Escalate / ask / hide under a daily budget for cloud microservice on-call.",
    version="0.1.0",
)

OPEN_PATHS = {"/health"}
INGEST_PATHS = {"/ingest/alerts", "/ingest/alertmanager"}


def _auth_settings() -> tuple[str, str] | None:
    """Login is on when APP_PASSWORD is set; APP_USER defaults to "oncall"."""
    password = os.environ.get("APP_PASSWORD")
    if not password:
        return None
    return os.environ.get("APP_USER", "oncall"), password


def _credentials_ok(header: str | None, user: str, password: str) -> bool:
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:].strip()).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    given_user, _, given_password = decoded.partition(":")
    user_ok = secrets.compare_digest(given_user.encode(), user.encode())
    password_ok = secrets.compare_digest(given_password.encode(), password.encode())
    return user_ok and password_ok


def _ingest_token_ok(request: Request) -> bool | None:
    """None when INGEST_TOKEN is unset. Accepts `Authorization: Bearer <token>`
    (Alertmanager `http_config.authorization`) or `?token=<token>`."""
    token = os.environ.get("INGEST_TOKEN")
    if not token:
        return None
    header = request.headers.get("authorization") or ""
    given = header[7:].strip() if header.lower().startswith("bearer ") else request.query_params.get("token", "")
    return secrets.compare_digest(given.encode(), token.encode())


def _is_private(host: str | None) -> bool:
    """Loopback, private (RFC 1918, Docker bridge) and link-local count as private.
    A host that is not an IP address (test client, Unix socket) counts as local."""
    if not host:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return ip.is_loopback or ip.is_private or ip.is_link_local


@app.middleware("http")
async def require_login(request: Request, call_next):
    if request.url.path in INGEST_PATHS:
        ok = _ingest_token_ok(request)
        if ok is False:
            return Response("Bad ingest token", status_code=401)
        if ok is True:
            return await call_next(request)
    settings = _auth_settings()
    if settings is None:
        # No password: serve this machine and private networks only, unless the
        # operator says otherwise. A public address must never reach an open queue.
        client = request.client.host if request.client else None
        if (
            request.url.path not in OPEN_PATHS
            and not _is_private(client)
            and os.environ.get("ALLOW_OPEN_ACCESS") != "1"
        ):
            return Response(
                "Set APP_PASSWORD before exposing this app (or ALLOW_OPEN_ACCESS=1).",
                status_code=403,
            )
        return await call_next(request)
    if request.url.path in OPEN_PATHS:
        return await call_next(request)
    if not _credentials_ok(request.headers.get("authorization"), *settings):
        return Response(
            "Login required",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="On-call alert queue"'},
        )
    return await call_next(request)


static_dir = APP_DIR / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


class RunRequest(BaseModel):
    mode: str = Field("sample", description="sample | gaia | ingest")
    budget_k: int = 20
    shadow_mode: bool | None = None


class IngestAlert(BaseModel):
    alert_id: str | None = None
    timestamp: str
    source: str
    alert_type: str | None = "info"
    severity: str = "info"
    message: str = ""
    fault_id: str | None = None


class FeedbackRequest(BaseModel):
    kind: str = Field(..., description="alert | jump")
    id: int
    verdict: str | None = Field(None, description="real | noise | null to clear")


class IngestRequest(BaseModel):
    alerts: list[IngestAlert]
    score_now: bool = False
    budget_k: int = 20


def _connector_status() -> dict:
    """What is wired. Secrets (keys, passwords) are never returned."""
    prom = (os.environ.get("PROM_URL") or "").rstrip("/")
    stats = STORE.buffer_stats()
    shadow = os.environ.get("SHADOW_MODE", "1") != "0"
    pd_on = bool(os.environ.get("PAGERDUTY_ROUTING_KEY"))
    return {
        "shadow_mode": shadow,
        "login_required": _auth_settings() is not None,
        "ingest_token_required": bool(os.environ.get("INGEST_TOKEN")),
        "webhook": {
            "ready": True,
            "path": "/ingest/alertmanager",
            "also": "/ingest/alerts",
            "buffered_batches": stats["buffered_batches"],
            "last_ingest_at": stats["last_ingest_at"],
        },
        "prometheus": {
            "configured": bool(prom),
            "url": prom or None,
            "query_set": bool(os.environ.get("PROM_QUERY")),
            "service_label": os.environ.get("PROM_SERVICE_LABEL") or None,
            "probe": probe_prometheus(prom or None) if prom else {"reachable": False, "error": "PROM_URL is not set"},
        },
        "pagerduty": {
            "configured": pd_on,
            "will_page": pd_on and not shadow,
            "last": (stats["last_job"] or {}).get("pagerduty"),
        },
        "last_job": stats["last_job"],
    }


@app.get("/health")
def health():
    status = _connector_status()
    return {
        "status": "ok",
        "shadow_mode": status["shadow_mode"],
        "login_required": status["login_required"],
        "pagerduty_configured": status["pagerduty"]["configured"],
    }


@app.get("/connectors")
def connectors():
    return _connector_status()


@app.post("/jobs/run")
def jobs_run(body: RunRequest):
    try:
        if body.mode == "ingest":
            rows, last_id = STORE.peek_buffer()
            if not rows:
                raise HTTPException(400, "Ingest buffer is empty")
            result = run_job(
                STORE,
                mode="ingest",
                budget_k=body.budget_k,
                shadow_mode=body.shadow_mode,
                alert_rows=rows,
            )
            STORE.ack_buffer(last_id)  # only after the job was saved
        else:
            result = run_job(
                STORE,
                mode=body.mode,
                budget_k=body.budget_k,
                shadow_mode=body.shadow_mode,
            )
        return result
    except HTTPException:
        raise
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — surface to UI
        raise HTTPException(500, str(exc)) from exc


@app.get("/queue")
def queue(
    job_id: str | None = None,
    day: str | None = None,
    action: str | None = Query(None, description="escalate|review|suppress|all"),
    limit: int = 500,
):
    job_id = job_id or STORE.latest_job_id()
    if not job_id:
        raise HTTPException(404, "No jobs yet. Run sample or GAIA first.")
    meta = STORE.job_meta(job_id)
    if meta is None:
        raise HTTPException(404, f"Unknown job_id {job_id}")
    rows = STORE.get_queue(job_id, day=day, action=action, limit=limit)
    jumps, jumps_total = STORE.get_jumps(job_id, day=day, per_day=meta["budget_k"])
    return {
        "job": meta,
        "days": STORE.list_days(job_id),
        "items": rows,
        "count": len(rows),
        "jumps": jumps,
        "jumps_total": jumps_total,
        "feedback": STORE.feedback_summary(),
        "model": model_status(),
    }


@app.post("/feedback")
def feedback(body: FeedbackRequest):
    try:
        return STORE.set_feedback(body.kind, body.id, body.verdict)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/model")
def model_status():
    saved = load_model()
    if saved is None:
        return {"saved": False}
    return {"saved": True, **saved.status(STORE.feedback_summary()["real"])}


@app.get("/feedback/faults.csv")
def feedback_faults():
    """Confirmed items as fault windows; upload with the next alerts to retrain."""
    columns = ["fault_id", "start", "end", "component", "description"]
    lines = [",".join(columns)]
    for fault in STORE.feedback_faults():
        lines.append(",".join(str(fault[c]).replace(",", ";") for c in columns))
    return Response(
        "\n".join(lines) + "\n",
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="faults.csv"'},
    )


async def _save_upload(upload: UploadFile | None, path: Path) -> Path | None:
    if upload is None or not upload.filename:
        return None
    path.write_bytes(await upload.read())
    return path


@app.post("/jobs/upload")
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
            result = run_job(
                STORE,
                mode="upload",
                budget_k=budget_k,
                shadow_mode=shadow_mode,
                alerts_csv=alerts_path,
                metrics_path=metrics_path,
                faults_path=faults_path,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, str(exc)) from exc
    return result


@app.post("/ingest/alerts")
@app.post("/ingest/alertmanager")
async def ingest_alerts(request: Request):
    """Accept our `{alerts: [...]}` body or a raw Alertmanager webhook."""
    try:
        payload = await request.json()
        rows = normalize_ingest(payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Invalid JSON: {exc}") from exc
    if not rows:
        return {"buffered_batches": 0, "received": 0, "note": "no firing alerts"}
    score_now = False
    budget_k = 20
    if isinstance(payload, dict) and not looks_like_alertmanager(payload):
        score_now = bool(payload.get("score_now"))
        budget_k = int(payload.get("budget_k") or 20)
    if request.query_params.get("score_now") in ("1", "true", "yes"):
        score_now = True
    if request.query_params.get("budget_k"):
        budget_k = int(request.query_params["budget_k"])
    buffered = STORE.buffer_alerts(rows)
    response: dict = {"buffered_batches": buffered, "received": len(rows)}
    if score_now:
        rows_now, last_id = STORE.peek_buffer()
        try:
            result = run_job(
                STORE,
                mode="ingest",
                budget_k=budget_k,
                alert_rows=rows_now,
            )
        except Exception as exc:  # noqa: BLE001 — rows stay buffered for the next try
            raise HTTPException(400, f"{exc} (alerts kept in the buffer)") from exc
        STORE.ack_buffer(last_id)
        response["job"] = result
    return response


@app.get("/", response_class=HTMLResponse)
def index():
    html_path = static_dir / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Evaluate my data: upload -> tune + evaluate -> use the settings -> score

EVAL_DIR = APP_DIR / "data" / "evals"


def _eval_folder(eval_id: str) -> Path:
    if not eval_id.isalnum():
        raise HTTPException(400, "Bad evaluation id")
    folder = EVAL_DIR / eval_id
    if not folder.exists():
        raise HTTPException(404, f"Unknown evaluation {eval_id}")
    return folder


@app.get("/evaluate", response_class=HTMLResponse)
def evaluate_page():
    return HTMLResponse((static_dir / "evaluate.html").read_text(encoding="utf-8"))


@app.get("/datasets")
def list_example_datasets():
    return {"datasets": example_data.list_datasets()}


@app.post("/datasets/fetch")
def fetch_example_datasets(body: dict | None = None):
    """Download NAB categories (needs internet)."""
    wanted = (body or {}).get("categories") or None
    try:
        return {"downloaded": example_data.fetch_nab(wanted), "datasets": example_data.list_datasets()}
    except KeyError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — offline, proxy, ...
        raise HTTPException(502, f"Could not download NAB: {type(exc).__name__}: {exc}") from exc


@app.post("/evaluate")
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
        paths = {role: folder / f"{role}.csv" for role in ("metrics", "incidents", "events")}
        paths = {role: p if p.exists() else None for role, p in paths.items()}
        try:
            k_list = tuple(int(x) for x in budgets.replace(" ", "").split(",") if x)
            report = evaluate(paths["metrics"], paths["incidents"], paths["events"], budgets=k_list or (5, 10, 20),
                              train_fraction=min(max(train_fraction, 0.05), 0.9),
                              default_incident_minutes=incident_minutes, day_offset_hours=day_offset_hours)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        data = report.to_dict()
        suggested = live_setting(paths["metrics"], report)
        (folder / "report.json").write_text(json.dumps({"report": data, "suggested": suggested,
                                                        "dataset": dataset}, default=str), encoding="utf-8")
        return {"eval_id": eval_id, "dataset": dataset, "report": data, "suggested": suggested}
    except HTTPException:
        shutil.rmtree(folder, ignore_errors=True)
        raise


@app.get("/settings")
def get_settings():
    saved = load_saved()
    if not saved:
        return {"saved": False, "live": "built-in memory-jump rule (JUMP_* environment variables)"}
    return {"saved": True, **saved}


@app.post("/settings")
def apply_settings(body: dict):
    """Use the settings an evaluation suggested for the live queue."""
    eval_id = str(body.get("eval_id") or "")
    stored = json.loads((_eval_folder(eval_id) / "report.json").read_text(encoding="utf-8"))
    saved = save_saved({**stored["suggested"], "eval_id": eval_id, "applied_at": pd_now()})
    return {"saved": True, **saved}


@app.delete("/settings")
def reset_settings():
    clear_saved()
    return {"saved": False, "live": "built-in memory-jump rule"}


@app.post("/evaluate/{eval_id}/score")
def score_evaluated(eval_id: str, body: dict | None = None):
    """Run the live queue on the evaluated files (every day, all items), for the Queue page."""
    folder = _eval_folder(eval_id)
    budget_k = int((body or {}).get("budget_k") or 20)
    events = folder / "events.csv"
    incidents = folder / "incidents.csv"
    try:
        return run_job(
            STORE,
            mode="upload",
            budget_k=budget_k,
            alerts_csv=events if events.exists() else None,
            metrics_path=folder / "metrics.csv",
            faults_path=incidents if incidents.exists() else None,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def pd_now() -> str:
    import pandas as pd

    return pd.Timestamp.now("UTC").isoformat()


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    html_path = static_dir / "dashboard.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


def create_app() -> FastAPI:
    return app
