"""Per-company PrometheusRules. Noisy companies fire on small moves; strict ones do not.

Latency and error rules use the collector's spanmetrics names. CPU, memory and
restarts use kube-prometheus metrics, so they still fire when an app has no
traces (Sock Shop). Thresholds are intentionally sensitive on the noisy profile.
"""
from __future__ import annotations

from .companies import Company

# Explicit so a chart bump does not silently retarget the rules.
SPAN_LATENCY = "traces_spanmetrics_duration_milliseconds_bucket"
SPAN_CALLS = "traces_spanmetrics_calls_total"

_NOISY = {
    "latency_ms": 150,
    "latency_for": "1m",
    "error_ratio": 0.005,
    "error_for": "1m",
    "cpu": 0.05,
    "cpu_for": "2m",
    "memory": 80 * 1024 * 1024,
    "memory_for": "5m",
    "restart_for": "5m",
    "include_resources": True,
}
_STRICT = {
    "latency_ms": 800,
    "latency_for": "10m",
    "error_ratio": 0.05,
    "error_for": "10m",
    "cpu": 0.8,
    "cpu_for": "15m",
    "memory": 512 * 1024 * 1024,
    "memory_for": "15m",
    "restart_for": "15m",
    "include_resources": False,
}


def profile(sensitivity: str) -> dict:
    if sensitivity == "noisy":
        return dict(_NOISY)
    if sensitivity == "strict":
        return dict(_STRICT)
    raise ValueError(f"unknown sensitivity {sensitivity}")


def _pod_sum(expr: str, namespace: str) -> str:
    """One PromQL line. A multi-line literal inside `expr: |` breaks YAML indentation."""
    inner = expr.replace("NAMESPACE", namespace)
    return (
        "sum by (service) (label_replace(sum by (pod) ("
        + inner
        + '), "service", "$1", "pod", "^(.*)-[a-z0-9]{5,10}-[a-z0-9]{5}$"))'
    )


def render_company(company: Company) -> str:
    limits = profile(company.sensitivity)
    ns = company.namespace
    cid = company.id
    groups = [
        _latency(cid, limits),
        _errors(cid, limits),
        _restarts(cid, ns, limits),
    ]
    if limits["include_resources"]:
        groups.append(_cpu(cid, ns, limits))
        groups.append(_memory(cid, ns, limits))
    body = "\n".join(groups)
    return f"""\
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: homelab-{cid}
  namespace: observability
  labels:
    app: homelab
    company: {cid}
    sensitivity: {company.sensitivity}
spec:
  groups:
{body}
"""


def render_all(companies: list[Company]) -> str:
    parts = [render_company(company) for company in companies]
    return "\n---\n".join(parts) + ("\n" if parts else "")


def _labels(company: str, severity: str) -> str:
    # The company id is constant. CPU and restart series have no company label.
    return (
        "          labels:\n"
        f"            severity: {severity}\n"
        f"            company: {company}\n"
        f"            service: '{company}/{{{{ $labels.service }}}}'\n"
        "          annotations:\n"
        '            summary: "homelab {{ $labels.alertname }} on {{ $labels.service }}"\n'
    )


def _latency(company: str, limits: dict) -> str:
    return f"""\
    - name: homelab-{company}-latency
      rules:
        - alert: HomelabLatencyP95High
          expr: |
            histogram_quantile(0.95,
              sum by (company, service, le) (
                rate({SPAN_LATENCY}{{company="{company}"}}[5m])
              )
            ) > {limits["latency_ms"]}
          for: {limits["latency_for"]}
{_labels(company, "warning")}"""


def _errors(company: str, limits: dict) -> str:
    return f"""\
    - name: homelab-{company}-errors
      rules:
        - alert: HomelabErrorRateHigh
          expr: |
            (
              sum by (company, service) (rate({SPAN_CALLS}{{company="{company}", status_code="STATUS_CODE_ERROR"}}[5m]))
              /
              sum by (company, service) (rate({SPAN_CALLS}{{company="{company}"}}[5m]))
            ) > {limits["error_ratio"]}
          for: {limits["error_for"]}
{_labels(company, "critical")}"""


def _cpu(company: str, namespace: str, limits: dict) -> str:
    expr = _pod_sum(
        'rate(container_cpu_usage_seconds_total{namespace="NAMESPACE", container!="", container!="POD", image!=""}[3m])',
        namespace,
    )
    return f"""\
    - name: homelab-{company}-cpu
      rules:
        - alert: HomelabCpuHigh
          expr: |
            {expr} > {limits["cpu"]}
          for: {limits["cpu_for"]}
{_labels(company, "warning")}"""


def _memory(company: str, namespace: str, limits: dict) -> str:
    expr = _pod_sum(
        'container_memory_working_set_bytes{namespace="NAMESPACE", container!="", container!="POD", image!=""}',
        namespace,
    )
    return f"""\
    - name: homelab-{company}-memory
      rules:
        - alert: HomelabMemoryHigh
          expr: |
            {expr} > {limits["memory"]}
          for: {limits["memory_for"]}
{_labels(company, "warning")}"""


def _restarts(company: str, namespace: str, limits: dict) -> str:
    expr = _pod_sum(
        'increase(kube_pod_container_status_restarts_total{namespace="NAMESPACE"}[15m])',
        namespace,
    )
    return f"""\
    - name: homelab-{company}-restarts
      rules:
        - alert: HomelabRestarted
          expr: |
            {expr} > 0
          for: {limits["restart_for"]}
{_labels(company, "warning")}"""
