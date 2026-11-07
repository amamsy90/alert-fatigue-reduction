"""A small fleet for the live soak: Prometheus scrapes it, faults are injected in real time.

Six services expose `container_memory_working_set_bytes{container="svcN"}` and
`fleet_error_ratio{service="svcN"}`. On a schedule it injects, in real time:

* a fault: +1 GB for 10 minutes on one service (the GAIA shape; ground truth is kept)
* a benign bump: +0.25 GB for 5 minutes (below the 0.5 GB jump rule; not a fault)
* an error burst: error ratio 0.5 for a minute (an alert that is never a fault)

`/truth` lists the injected faults so the soak runner can mark the queue like an engineer
who knows what happened. Nothing here is a real workload: the fleet is simulated, but the
Prometheus scraping, Alertmanager, webhook, ingest, jobs and retraining around it are real.
"""
from __future__ import annotations

import argparse
import json
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVICES = [f"svc{i}" for i in range(1, 7)]
# Baselines in bytes. Services above 2.2 GB cross the 3.2 GB HighMemory rule during a fault
# (a log-style alert); the others change only in memory, like most GAIA faults.
BASELINE = dict(zip(SERVICES, (1.6e9, 1.9e9, 2.1e9, 2.3e9, 2.6e9, 2.9e9)))
FAULT_BYTES, FAULT_SECONDS = 1.0e9, 600
BENIGN_BYTES, BENIGN_SECONDS = 0.25e9, 300
BURST_SECONDS = 60
# A new fault needs a quiet service for longer than the jump rule's 15-minute median window; a fault
# right after another has no jump to see, because the level it rises from is already high.
FAULT_COOLDOWN = 20 * 60

LOCK = threading.Lock()
active: dict[str, list[tuple[float, float, float]]] = {s: [] for s in SERVICES}  # (start, end, bytes)
bursts: dict[str, float] = {}  # service -> end time
truth: list[dict] = []


def memory(service: str, now: float, rng: random.Random) -> float:
    extra = sum(b for start, end, b in active[service] if start <= now < end)
    wobble = 8e6 * ((now // 30) % 7) + rng.gauss(0, 12e6)
    return BASELINE[service] + extra + wobble


def schedule(args: argparse.Namespace) -> None:
    rng = random.Random(args.seed)
    t0 = time.time()
    next_fault = t0 + args.warmup
    next_benign = t0 + args.warmup / 2
    next_burst = t0 + args.warmup / 3
    while True:
        now = time.time()
        with LOCK:
            if now >= next_fault:
                free = [s for s in SERVICES if not any(e + (FAULT_COOLDOWN if b == FAULT_BYTES else 0) > now
                                                       for _, e, b in active[s])]
                if free:
                    service = rng.choice(free)
                    active[service].append((now, now + FAULT_SECONDS, FAULT_BYTES))
                    truth.append({"id": f"f{len(truth) + 1}", "service": service, "start": now,
                                  "end": now + FAULT_SECONDS})
                next_fault = now + args.fault_every * rng.uniform(0.7, 1.3)
            if now >= next_benign:
                service = rng.choice(SERVICES)
                active[service].append((now, now + BENIGN_SECONDS, BENIGN_BYTES))
                next_benign = now + 780 * rng.uniform(0.7, 1.3)
            if now >= next_burst:
                bursts[rng.choice(SERVICES)] = now + BURST_SECONDS
                next_burst = now + 540 * rng.uniform(0.7, 1.3)
        time.sleep(1)


class Handler(BaseHTTPRequestHandler):
    rng = random.Random(1)

    def log_message(self, *args) -> None:  # keep the container log quiet
        pass

    def _send(self, body: str, kind: str = "text/plain") -> None:
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        now = time.time()
        if self.path == "/truth":
            with LOCK:
                self._send(json.dumps({"now": now, "faults": truth}), "application/json")
            return
        lines = ["# TYPE container_memory_working_set_bytes gauge", "# TYPE fleet_error_ratio gauge"]
        with LOCK:
            for s in SERVICES:
                lines.append(f'container_memory_working_set_bytes{{container="{s}"}} {memory(s, now, self.rng):.0f}')
            for s in SERVICES:
                ratio = 0.5 if bursts.get(s, 0) > now else 0.02
                lines.append(f'fleet_error_ratio{{service="{s}"}} {ratio}')
        self._send("\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--warmup", type=float, default=1200, help="seconds before the first fault")
    parser.add_argument("--fault-every", type=float, default=300, help="mean seconds between faults")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    threading.Thread(target=schedule, args=(args,), daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
