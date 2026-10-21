"""Turn Prometheus, Loki and Tempo payloads into the app's CSV shapes.

Every row carries a company id. Service names are the demo names, so they
match ground truth (`company/service`). Parsing is pure: the shell fetches
the HTTP bodies and this module does not open a socket.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .companies import Company, load_companies
from .names import service_from_pod, tenant_source

ALERT_COLUMNS = ("alert_id", "timestamp", "source", "alert_type", "severity", "message", "company")
METRIC_COLUMNS = ("timestamp", "value", "service", "company", "metric")
LOG_COLUMNS = ("timestamp", "source", "severity", "message", "company")
SPAN_COLUMNS = (
    "traceID",
    "spanID",
    "parentSpanID",
    "serviceName",
    "startTimeMillis",
    "duration",
    "statusCode",
    "company",
)
MEMORY_METRIC = "memory_working_set_bytes"


def _stamp(seconds: float) -> str:
    return datetime.fromtimestamp(float(seconds), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _company_by_namespace(companies: list[Company]) -> dict[str, Company]:
    return {company.namespace: company for company in companies}


def _catalog(company: Company) -> list[str]:
    return company.services()


def company_of_namespace(namespace: str, companies: list[Company]) -> str:
    found = _company_by_namespace(companies).get(namespace)
    return found.id if found else "cluster"


def canonical_service(raw: str, company: Company | None) -> str:
    text = (raw or "").strip()
    if not text:
        return "unknown"
    catalog = company.services() if company else []
    if "/" in text:
        head, tail = text.split("/", 1)
        if company and head == company.id:
            text = tail
    return service_from_pod(text, catalog) if catalog else text


def firing_episodes(values: list[list]) -> list[tuple[float, float]]:
    """Inclusive start of each stretch where the sample is at least 1."""
    episodes: list[tuple[float, float]] = []
    start: float | None = None
    previous = None
    for pair in values:
        if len(pair) < 2:
            continue
        stamp = float(pair[0])
        try:
            on = float(pair[1]) >= 1
        except (TypeError, ValueError):
            on = False
        if on and start is None:
            start = stamp
        if not on and start is not None:
            episodes.append((start, stamp))
            start = None
        previous = stamp
    if start is not None and previous is not None:
        episodes.append((start, previous))
    return episodes


def alerts_from_prometheus(payload: dict, companies: list[Company]) -> list[dict[str, str]]:
    result = ((payload.get("data") or {}).get("result")) or []
    by_ns = _company_by_namespace(companies)
    rows: list[dict[str, str]] = []
    for series in result:
        metric = series.get("metric") or {}
        namespace = str(metric.get("namespace") or "")
        company = by_ns.get(namespace)
        company_id = str(metric.get("company") or (company.id if company else "cluster"))
        if company is None:
            company = next((item for item in companies if item.id == company_id), None)
        raw_service = str(metric.get("service") or metric.get("pod") or metric.get("container") or "unknown")
        service = canonical_service(raw_service, company)
        name = str(metric.get("alertname") or "alert")
        severity = str(metric.get("severity") or "warning").lower()
        for start, _end in firing_episodes(series.get("values") or []):
            started = _stamp(start)
            source = tenant_source(company_id, service)
            digest = hashlib.sha1(f"{source}|{name}|{started}".encode()).hexdigest()[:16]
            rows.append(
                {
                    "alert_id": digest,
                    "timestamp": started,
                    "source": source,
                    "alert_type": name,
                    "severity": severity,
                    "message": f"{name} on {source}",
                    "company": company_id,
                }
            )
    return rows


def metrics_from_prometheus(payload: dict, companies: list[Company], metric_name: str = MEMORY_METRIC) -> list[dict]:
    result = ((payload.get("data") or {}).get("result")) or []
    by_ns = _company_by_namespace(companies)
    rows: list[dict] = []
    for series in result:
        metric = series.get("metric") or {}
        namespace = str(metric.get("namespace") or "")
        company = by_ns.get(namespace)
        if company is None:
            continue
        raw = str(metric.get("service") or metric.get("pod") or "")
        service = canonical_service(raw, company)
        for pair in series.get("values") or []:
            if len(pair) < 2:
                continue
            try:
                value = float(pair[1])
            except (TypeError, ValueError):
                continue
            rows.append(
                {
                    "timestamp": _stamp(float(pair[0])),
                    "value": value,
                    "service": tenant_source(company.id, service),
                    "company": company.id,
                    "metric": metric_name,
                }
            )
    return _sum_metrics(rows)


def _sum_metrics(rows: list[dict]) -> list[dict]:
    """One value per service. Replica pods that share a name are added together."""
    if not rows:
        return []
    frame = pd.DataFrame(rows)
    grouped = frame.groupby(["timestamp", "service", "company", "metric"], as_index=False)["value"].sum()
    return grouped.to_dict("records")


def _severity(line: str) -> str:
    text = line.lower()
    if any(word in text for word in ("fatal", "critical", "error", "exception")):
        return "error"
    if "warn" in text:
        return "warning"
    return "info"


def logs_from_loki(payload: dict, companies: list[Company]) -> list[dict[str, str]]:
    result = ((payload.get("data") or {}).get("result")) or []
    by_ns = _company_by_namespace(companies)
    rows: list[dict[str, str]] = []
    for stream in result:
        labels = stream.get("stream") or {}
        namespace = str(labels.get("namespace") or "")
        company = by_ns.get(namespace)
        company_id = company.id if company else "cluster"
        raw = str(labels.get("service") or labels.get("app") or labels.get("pod") or "unknown")
        service = canonical_service(raw, company)
        source = tenant_source(company_id, service)
        for pair in stream.get("values") or []:
            if len(pair) < 2:
                continue
            nanos = int(pair[0])
            message = str(pair[1]).replace("\n", " ")[:300]
            rows.append(
                {
                    "timestamp": _stamp(nanos / 1e9),
                    "source": source,
                    "severity": _severity(message),
                    "message": message,
                    "company": company_id,
                }
            )
    return rows


def _attr(attributes: list | None, key: str) -> str:
    for item in attributes or []:
        if item.get("key") == key:
            value = item.get("value") or {}
            for field in ("stringValue", "intValue", "boolValue"):
                if field in value:
                    return str(value[field])
    return ""


def _error_code(status: dict | None) -> str:
    code = str((status or {}).get("code") or "")
    if code in {"2", "STATUS_CODE_ERROR", "ERROR"}:
        return "ERROR"
    return "OK"


def spans_from_otlp(payload: dict, default_company: str = "unknown") -> list[dict]:
    batches = payload.get("resourceSpans") or payload.get("batches") or []
    if isinstance(payload.get("trace"), dict):
        batches = payload["trace"].get("resourceSpans") or batches
    rows: list[dict] = []
    for batch in batches:
        resource = (batch.get("resource") or {}).get("attributes")
        service = _attr(resource, "service.name") or "unknown"
        company = _attr(resource, "company") or default_company
        scopes = batch.get("scopeSpans") or batch.get("instrumentationLibrarySpans") or []
        for scope in scopes:
            for span in scope.get("spans") or []:
                start = int(span.get("startTimeUnixNano") or 0)
                end = int(span.get("endTimeUnixNano") or start)
                if start <= 0:
                    continue
                rows.append(
                    {
                        "traceID": str(span.get("traceId") or ""),
                        "spanID": str(span.get("spanId") or ""),
                        "parentSpanID": str(span.get("parentSpanId") or ""),
                        "serviceName": tenant_source(company, service),
                        "startTimeMillis": start // 1_000_000,
                        "duration": max((end - start) // 1_000_000, 0),
                        "statusCode": _error_code(span.get("status")),
                        "company": company,
                    }
                )
    return rows


def memory_query(namespace: str) -> str:
    return (
        "sum by (namespace, pod) ("
        f'container_memory_working_set_bytes{{namespace="{namespace}", container!="", container!="POD", image!=""}}'
        ")"
    )


def alerts_query() -> str:
    return 'ALERTS{alertstate="firing"}'


def trace_ids(payload: dict) -> list[str]:
    """Trace ids from a Tempo `/api/search` payload."""
    found: list[str] = []
    for item in payload.get("traces") or []:
        if not isinstance(item, dict):
            continue
        trace_id = str(item.get("traceID") or item.get("traceId") or "")
        if trace_id:
            found.append(trace_id)
    return found


def write_table(path: Path, rows: list[dict], columns: tuple[str, ...], keys: list[str]) -> int:
    """Append rows and drop duplicates. Returns the number of new rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = pd.DataFrame(rows, columns=list(columns))
    if path.exists() and path.stat().st_size:
        old = pd.read_csv(path)
    else:
        old = pd.DataFrame(columns=list(columns))
    had = len(old)
    combined = pd.concat([old, fresh], ignore_index=True)
    combined = combined.drop_duplicates(keys, keep="last")
    combined.to_csv(path, index=False)
    return max(0, len(combined) - had)


def dump_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    """Print a PromQL query, or append parsed rows to a CSV. No network."""
    parser = argparse.ArgumentParser(description="Parse telemetry payloads. Does not open a socket.")
    sub = parser.add_subparsers(dest="command", required=True)
    query = sub.add_parser("query", help="Print the PromQL the collector loop should run.")
    query.add_argument("--kind", choices=("alerts", "memory"), required=True)
    query.add_argument("--namespace", default="")
    ingest = sub.add_parser("ingest", help="Read a JSON payload from --input or stdin and append a CSV.")
    ingest.add_argument("--kind", choices=("alerts", "metrics", "logs", "spans"), required=True)
    ingest.add_argument("--input", type=Path)
    ingest.add_argument("--out", type=Path, required=True)
    ingest.add_argument("--companies-file", type=Path)
    ingest.add_argument("--metric", default=MEMORY_METRIC)
    ids = sub.add_parser("ids", help="Print Tempo search trace ids, one per line.")
    ids.add_argument("--input", type=Path)
    args = parser.parse_args(argv)
    if args.command == "query":
        if args.kind == "alerts":
            print(alerts_query())
            return
        if not args.namespace:
            raise SystemExit("a memory query needs --namespace")
        print(memory_query(args.namespace))
        return
    raw = args.input.read_text(encoding="utf-8") if args.input else sys.stdin.read()
    if args.command == "ids":
        payload = json.loads(raw) if raw.strip() else {}
        for trace_id in trace_ids(payload):
            print(trace_id)
        return
    payload = json.loads(raw) if raw.strip() else {}
    companies = load_companies(args.companies_file)
    if args.kind == "alerts":
        rows = alerts_from_prometheus(payload, companies)
        added = write_table(args.out, rows, ALERT_COLUMNS, ["alert_id"])
    elif args.kind == "metrics":
        rows = metrics_from_prometheus(payload, companies, args.metric)
        added = write_table(args.out, rows, METRIC_COLUMNS, ["timestamp", "service", "metric"])
    elif args.kind == "logs":
        rows = logs_from_loki(payload, companies)
        added = write_table(args.out, rows, LOG_COLUMNS, ["timestamp", "source", "message"])
    else:
        rows = spans_from_otlp(payload)
        added = write_table(args.out, rows, SPAN_COLUMNS, ["traceID", "spanID"])
    print(f"added {added}")


if __name__ == "__main__":
    main()
