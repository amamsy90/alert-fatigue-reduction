"""Production monitoring: request, job and paging metrics, plus AI and data health.

`/metrics` serves Prometheus text format (scrape it with the team's Prometheus;
no extra dependency). `/monitoring` returns the same signals as JSON with a
pass / warn verdict against the targets in docs/monitoring.md, so a person can
see at a glance whether the service, the model and the data look healthy.

Counters live in this process (one worker, ADR 0003) and reset on restart;
the model and data signals are computed from the database at scrape time.
"""
from __future__ import annotations

import math
import re
import threading
import time
from collections import defaultdict

LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

# Targets (docs/monitoring.md). A signal outside its target makes /monitoring warn.
TARGETS: dict[str, float] = {
    "p95_latency_seconds": 1.0,        # interactive routes, excluding jobs and evaluations
    "error_rate": 0.02,                # 5xx share of requests
    "precision_from_marks_min": 0.7,   # share of marked items the on-call engineer called Real
    "min_marks_for_precision": 20,     # fewer marks than this: not enough to judge
    "gap_items_max": 3,                # monitoring gaps shown in the last job
}
# Last ingest batch size vs the median of earlier ones: outside this band is drift.
ALERT_VOLUME_BAND = (0.5, 2.0)
# Last job metric median vs the median of earlier jobs: same band as alert volume.
METRIC_VALUE_BAND = (0.5, 2.0)

_ID = re.compile(r"/(v\d{4}|[0-9a-f]{12,}|\d+)(?=/|$)")
_SKIP = {"/metrics", "/health", "/favicon.ico"}
_SLOW_ROUTES = ("/jobs/", "/evaluate", "/ingest/")


class Metrics:
    """Thread-safe counters and histograms, rendered in Prometheus text format."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counters: dict[tuple[str, tuple], float] = defaultdict(float)
        self.hist: dict[tuple[str, tuple], list[float]] = {}
        self.help: dict[str, tuple[str, str]] = {}

    def inc(self, name: str, labels: dict | None = None, value: float = 1.0, doc: str = "") -> None:
        with self._lock:
            self.help.setdefault(name, ("counter", doc))
            self.counters[(name, tuple(sorted((labels or {}).items())))] += value

    def observe(self, name: str, seconds: float, labels: dict | None = None, doc: str = "") -> None:
        with self._lock:
            self.help.setdefault(name, ("histogram", doc))
            key = (name, tuple(sorted((labels or {}).items())))
            self.hist.setdefault(key, []).append(seconds)

    def quantile(self, name: str, q: float, match=lambda labels: True) -> float | None:
        with self._lock:
            values = sorted(v for (n, lab), vs in self.hist.items() if n == name and match(dict(lab)) for v in vs)
        if not values:
            return None
        return values[min(len(values) - 1, max(0, math.ceil(q * len(values)) - 1))]

    def total(self, name: str, match=lambda labels: True) -> float:
        with self._lock:
            return sum(v for (n, lab), v in self.counters.items() if n == name and match(dict(lab)))

    def reset(self) -> None:
        with self._lock:
            self.counters.clear()
            self.hist.clear()

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            names = sorted(self.help)
            for name in names:
                kind, doc = self.help[name]
                lines.append(f"# HELP {name} {doc or name}")
                lines.append(f"# TYPE {name} {kind}")
                if kind == "counter":
                    for (n, lab), v in sorted(self.counters.items()):
                        if n == name:
                            lines.append(f"{name}{_labels(lab)} {v:g}")
                else:
                    for (n, lab), vs in sorted(self.hist.items()):
                        if n != name:
                            continue
                        for bound in LATENCY_BUCKETS:
                            count = sum(1 for v in vs if v <= bound)
                            lines.append(f"{name}_bucket{_labels(lab + (('le', f'{bound:g}'),))} {count}")
                        lines.append(f"{name}_bucket{_labels(lab + (('le', '+Inf'),))} {len(vs)}")
                        lines.append(f"{name}_sum{_labels(lab)} {sum(vs):.6f}")
                        lines.append(f"{name}_count{_labels(lab)} {len(vs)}")
        return "\n".join(lines) + "\n"


def _labels(pairs: tuple) -> str:
    if not pairs:
        return ""
    body = ",".join(f'{k}="{str(v).replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"' for k, v in pairs)
    return "{" + body + "}"


METRICS = Metrics()


def route_label(path: str) -> str:
    """Collapse ids (v0001, evaluation ids, numbers) so labels stay few."""
    return _ID.sub("/{id}", path)


async def metrics_middleware(request, call_next):
    """Count every request and time it, by route template, method and status class."""
    path = request.url.path
    if path in _SKIP or path.startswith("/static/"):
        return await call_next(request)
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        labels = {"route": route_label(path), "method": request.method}
        METRICS.observe("oncall_http_request_duration_seconds", time.perf_counter() - start, labels,
                        "Request latency by route")
        METRICS.inc("oncall_http_requests_total", {**labels, "status": f"{status // 100}xx"},
                    doc="Requests by route, method and status class")


def record_job(mode: str, outcome: str, seconds: float) -> None:
    METRICS.inc("oncall_jobs_total", {"mode": mode, "outcome": outcome}, doc="Scoring jobs by mode and outcome")
    METRICS.observe("oncall_job_duration_seconds", seconds, {"mode": mode}, "Scoring job duration")


def record_page(channel: str, sent: int, failed: int) -> None:
    if sent:
        METRICS.inc("oncall_pages_total", {"channel": channel, "outcome": "sent"}, sent, "Pages by channel and outcome")
    if failed:
        METRICS.inc("oncall_pages_total", {"channel": channel, "outcome": "failed"}, failed, "Pages by channel and outcome")


def record_ingest(new: int, duplicates: int) -> None:
    METRICS.inc("oncall_ingest_alerts_total", {"result": "new"}, new, "Alerts received on /ingest")
    if duplicates:
        METRICS.inc("oncall_ingest_alerts_total", {"result": "duplicate"}, duplicates, "Alerts received on /ingest")


def record_llm(outcome: str, seconds: float) -> None:
    """One shadow-note attempt. ``off`` is not recorded: the flag is the default."""
    METRICS.inc(
        "oncall_llm_explain_total",
        {"outcome": outcome},
        doc="Shadow-note attempts by outcome (ok, timeout, fallback, error)",
    )
    METRICS.observe(
        "oncall_llm_explain_duration_seconds",
        max(0.0, seconds),
        doc="Shadow-note latency in seconds",
    )


def llm_rates() -> tuple[float | None, float | None, float]:
    """Timeout rate, fallback rate, and attempt count. Rates are None with no attempts."""
    attempts = METRICS.total("oncall_llm_explain_total")
    if not attempts:
        return None, None, 0.0
    timeouts = METRICS.total("oncall_llm_explain_total", lambda lab: lab.get("outcome") == "timeout")
    fallbacks = METRICS.total("oncall_llm_explain_total", lambda lab: lab.get("outcome") == "fallback")
    return timeouts / attempts, fallbacks / attempts, attempts


# ---------------------------------------------------------------------------
# Health computed at scrape time


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def _series_drift(jobs: list[dict], low: float, high: float) -> tuple[float | None, list[str]]:
    """Per series: the last job's recent median over the median of the same
    series in earlier jobs (at least 3). Returns the ratio farthest from 1, and
    the series outside [low, high]. (None, []) when no series has enough history."""
    per_job = [j["meta"].get("metric_medians") or {} for j in jobs if j["meta"].get("metric_medians")]
    if len(per_job) < 4:
        return None, []
    worst: float | None = None
    drifted: list[str] = []
    for series, value in per_job[0].items():
        earlier = [float(m[series]) for m in per_job[1:] if series in m]
        base = _median(earlier) if len(earlier) >= 3 else None
        if not base:
            continue
        ratio = float(value) / base
        # Farthest from 1 on a log scale, so halving and doubling weigh the same.
        # A series that dropped to zero is the worst case and stays so.
        if ratio <= 0:
            worst = ratio
        elif worst is None or (worst > 0 and abs(math.log(ratio)) > abs(math.log(worst))):
            worst = ratio
        if not low <= ratio <= high:
            drifted.append(series)
    return worst, sorted(drifted)


def health(store) -> dict:
    """Service, AI and data signals with a verdict against TARGETS."""
    fast = lambda lab: not lab.get("route", "").startswith(_SLOW_ROUTES)  # noqa: E731
    p95 = METRICS.quantile("oncall_http_request_duration_seconds", 0.95, fast)
    requests = METRICS.total("oncall_http_requests_total")
    errors = METRICS.total("oncall_http_requests_total", lambda lab: lab.get("status") == "5xx")
    error_rate = errors / requests if requests else None
    marks = store.feedback_summary()
    marked = marks["real"] + marks["noise"]
    precision = marks["real"] / marked if marked else None
    jobs = store.recent_jobs(limit=30)
    ingest_counts = [j["meta"].get("alert_count", 0) for j in jobs if j["source"] == "ingest"]
    last = jobs[0]["meta"] if jobs else {}
    volume_ratio = None
    if len(ingest_counts) >= 4:
        base = _median([float(c) for c in ingest_counts[1:]])
        if base:
            volume_ratio = ingest_counts[0] / base
    m_low, m_high = METRIC_VALUE_BAND
    metric_ratio, drifted = _series_drift(jobs, m_low, m_high)
    if metric_ratio is None:
        # Jobs saved before per-series medians: one fleet-wide median.
        metric_medians = [
            j["meta"].get("metric_median")
            for j in jobs
            if j["meta"].get("metric_median") is not None
        ]
        if len(metric_medians) >= 4:
            metric_base = _median([float(m) for m in metric_medians[1:]])
            if metric_base:
                metric_ratio = float(metric_medians[0]) / metric_base
    pages_failed = METRICS.total("oncall_pages_total", lambda lab: lab.get("outcome") == "failed")
    low, high = ALERT_VOLUME_BAND
    timeout_rate, fallback_rate, attempts = llm_rates()
    checks = {
        "latency": _check(p95, lambda v: v <= TARGETS["p95_latency_seconds"]),
        "errors": _check(error_rate, lambda v: v <= TARGETS["error_rate"]),
        "precision_from_marks": _check(
            precision if marked >= TARGETS["min_marks_for_precision"] else None,
            lambda v: v >= TARGETS["precision_from_marks_min"]),
        "alert_volume_drift": _check(volume_ratio, lambda v: low <= v <= high),
        "metric_value_drift": _check(metric_ratio, lambda v: m_low <= v <= m_high),
        "template_feature_drift": _template_feature_drift(jobs),
        "monitoring_gaps": _check(last.get("gap_items") if last else None, lambda v: v <= TARGETS["gap_items_max"]),
        "paging": _check(pages_failed, lambda v: v == 0),
    }
    status = "warn" if any(c["status"] == "warn" for c in checks.values()) else "ok"
    return {
        "status": status,
        "checks": checks,
        "service": {"requests": requests, "errors_5xx": errors, "error_rate": error_rate, "p95_latency_seconds": p95},
        "model": {"marks": marked, "real": marks["real"], "noise": marks["noise"], "precision_from_marks": precision,
                  "alert_precision": marks["alert"]["precision"], "jump_precision": marks["jump"]["precision"],
                  "last_model_used": last.get("model_used"), "last_label_source": last.get("label_source")},
        "data": {"last_job_alerts": last.get("alert_count"), "alert_volume_ratio": volume_ratio,
                 "metric_median_ratio": metric_ratio, "metric_drift_series": drifted,
                 "last_job_metric_median": last.get("metric_median"),
                 "last_job_gap_items": last.get("gap_items"), "last_job_has_metrics": last.get("has_metrics")},
        "targets": {**TARGETS, "alert_volume_band": list(ALERT_VOLUME_BAND),
                    "metric_value_band": list(METRIC_VALUE_BAND)},
        "accel": {"backend": _active_backend()},
        "llm": {**_llm_health(), "attempts": attempts, "timeout_rate": timeout_rate,
                "fallback_rate": fallback_rate},
        "gpu": {"memory_mib": _gpu_memory_mib()},
    }


def liveness_fields() -> dict:
    """Fields ``/health`` adds. No Prometheus probe and no GPU-memory query.

    Weight bytes are hashed only when explanations are on (see ``llm_health``).
    """
    return {"accel": {"backend": _active_backend()}, "llm": _llm_health()}


def _active_backend() -> str:
    from src.accel import active_backend

    return active_backend()


def _llm_health() -> dict:
    from product.llm import llm_health

    return llm_health()


def _gpu_memory_mib() -> float | None:
    from src.accel import gpu_memory_mib

    return gpu_memory_mib()


def _template_feature_drift(jobs: list[dict]) -> dict:
    """Skip when ``LLM_TEMPLATE_FEATURES`` is off. Otherwise the same band as alert volume.

    The value is the last job's mean ``tpl_prior`` over the median of earlier jobs.
    Fewer than four jobs, or a zero baseline, is not judged.
    """
    from src.template_features import template_features_enabled

    if not template_features_enabled():
        return {"status": "n/a", "value": None}
    values = [
        float(job["meta"]["template_prior_mean"])
        for job in jobs
        if job.get("meta", {}).get("template_prior_mean") is not None
    ]
    if len(values) < 4:
        return {"status": "n/a", "value": None}
    base = _median(values[1:])
    if not base:
        return {"status": "n/a", "value": None}
    return _check(values[0] / base, lambda v: ALERT_VOLUME_BAND[0] <= v <= ALERT_VOLUME_BAND[1])


def _check(value, ok) -> dict:
    if value is None:
        return {"status": "n/a", "value": None}
    return {"status": "ok" if ok(value) else "warn", "value": round(value, 4) if isinstance(value, float) else value}


def gauges(store) -> str:
    """Scrape-time gauges appended to /metrics."""
    h = health(store)
    buffer = store.buffer_stats()
    values = {
        "oncall_ingest_buffer_batches": (buffer["buffered_batches"], "Alert batches waiting for a scoring job"),
        "oncall_feedback_marks": (h["model"]["marks"], "Real and Noise marks recorded"),
        "oncall_precision_from_marks": (h["model"]["precision_from_marks"], "Share of marked items that were Real"),
        "oncall_alert_volume_ratio": (h["data"]["alert_volume_ratio"], "Last ingest batch size / median of earlier ones"),
        "oncall_metric_median_ratio": (h["data"]["metric_median_ratio"], "Last job metric median / median of earlier jobs"),
        "oncall_last_job_gap_items": (h["data"]["last_job_gap_items"], "Monitoring gaps shown in the last job"),
        "oncall_health_ok": (1 if h["status"] == "ok" else 0, "1 when every /monitoring check passes"),
        "oncall_llm_timeout_rate": (h["llm"]["timeout_rate"], "Share of shadow-note attempts that timed out"),
        "oncall_llm_fallback_rate": (h["llm"]["fallback_rate"], "Share of shadow-note attempts that fell back"),
        "oncall_gpu_memory_mebibytes": (h["gpu"]["memory_mib"], "GPU memory in use, when nvidia-smi answers"),
    }
    lines = []
    for name, (value, doc) in values.items():
        if value is None:
            continue
        lines += [f"# HELP {name} {doc}", f"# TYPE {name} gauge", f"{name} {float(value):g}"]
    return "\n".join(lines) + "\n"
