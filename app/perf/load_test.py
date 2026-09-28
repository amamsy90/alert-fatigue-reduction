"""Load test for a running on-call queue: latency, throughput and errors under concurrency.

    python perf/load_test.py --url http://127.0.0.1:8080 --users 20 --requests 400

Each simulated user sends a mix of the calls an on-call team makes: webhook
ingest (Alertmanager), the queue page, the Connectors page, /health and
/metrics. The script prints p50 / p95 / max latency per route, throughput and
the error rate, then checks them against the criteria in docs/test_plan.md and
exits 1 if any fails. Pass --user/--password when APP_PASSWORD is set and
--token when INGEST_TOKEN is set.
"""
from __future__ import annotations

import argparse
import itertools
import json
import statistics
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import httpx

CRITERIA = {
    "p95_seconds": 1.0,          # interactive routes (ingest, queue, connectors, health, metrics)
    "error_rate": 0.01,          # non-2xx/4xx-expected share
    "throughput_rps_min": 20.0,  # requests per second across all users
}
MIX = ("ingest", "queue", "connectors", "health", "metrics")


def _call(client: httpx.Client, kind: str, n: int, token: str | None) -> tuple[str, float, bool]:
    start = time.perf_counter()
    if kind == "ingest":
        alert = {"timestamp": f"2026-03-01T{n % 24:02d}:{n % 60:02d}:00Z", "source": f"svc{n % 7}",
                 "severity": "warning", "message": f"load test {n}"}
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        r = client.post("/ingest/alerts", json={"alerts": [alert]}, headers=headers)
        ok = r.status_code == 200
    elif kind == "queue":
        r = client.get("/queue")
        ok = r.status_code in (200, 404)  # 404 before the first job is expected
    else:
        r = client.get("/" + kind)
        ok = r.status_code == 200
    return kind, time.perf_counter() - start, ok


def run(url: str, users: int, requests: int, auth, token: str | None) -> dict:
    counter = itertools.count()
    with httpx.Client(base_url=url, auth=auth, timeout=30.0) as client:
        client.get("/health").raise_for_status()
        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=users) as pool:
            results = list(pool.map(lambda i: _call(client, MIX[i % len(MIX)], next(counter), token), range(requests)))
        elapsed = time.perf_counter() - start
    by_route: dict[str, list[float]] = defaultdict(list)
    errors = 0
    for kind, seconds, ok in results:
        by_route[kind].append(seconds)
        errors += not ok
    every = sorted(s for _, s, _ in results)

    def p(values: list[float], q: float) -> float:
        values = sorted(values)
        return values[min(len(values) - 1, int(q * len(values)))]

    return {
        "users": users,
        "requests": requests,
        "seconds": round(elapsed, 3),
        "throughput_rps": round(requests / elapsed, 1),
        "error_rate": round(errors / requests, 4),
        "p50_seconds": round(statistics.median(every), 4),
        "p95_seconds": round(p(every, 0.95), 4),
        "max_seconds": round(every[-1], 4),
        "routes": {k: {"p50": round(statistics.median(v), 4), "p95": round(p(v, 0.95), 4), "n": len(v)}
                   for k, v in sorted(by_route.items())},
    }


def verdict(report: dict) -> list[str]:
    failures = []
    if report["p95_seconds"] > CRITERIA["p95_seconds"]:
        failures.append(f"p95 {report['p95_seconds']}s > {CRITERIA['p95_seconds']}s")
    if report["error_rate"] > CRITERIA["error_rate"]:
        failures.append(f"error rate {report['error_rate']} > {CRITERIA['error_rate']}")
    if report["throughput_rps"] < CRITERIA["throughput_rps_min"]:
        failures.append(f"throughput {report['throughput_rps']} rps < {CRITERIA['throughput_rps_min']}")
    return failures


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    ap.add_argument("--users", type=int, default=20)
    ap.add_argument("--requests", type=int, default=400)
    ap.add_argument("--user", default="oncall")
    ap.add_argument("--password")
    ap.add_argument("--token")
    args = ap.parse_args()
    auth = (args.user, args.password) if args.password else None
    report = run(args.url, args.users, args.requests, auth, args.token)
    failures = verdict(report)
    report["criteria"] = CRITERIA
    report["passed"] = not failures
    print(json.dumps(report, indent=2))
    for f in failures:
        print("FAIL:", f, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
