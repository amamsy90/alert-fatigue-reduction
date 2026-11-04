"""Regression tests for the Oct 22 lab failures (collector E2BIG, stuck chaos
delete, dead port-forward, repeated helm upgrades, score-loop 400s)."""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import threading
from pathlib import Path

import pytest

HOMELAB = Path(__file__).resolve().parents[1]
SCRIPTS = HOMELAB / "scripts"


def _bash(script: str, env: dict | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
    full = {**os.environ, **(env or {})}
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=full, timeout=timeout)


# 1. collector: PYTHONPATH must not grow with every py() call -----------------

def test_py_does_not_grow_pythonpath():
    out = _bash(
        f'source "{SCRIPTS}/lib.sh"; HOMELAB_PYTHON=/usr/bin/true; '
        'py; first=${#PYTHONPATH}; for i in $(seq 1 3000); do py; done; echo "$first ${#PYTHONPATH}"'
    )
    assert out.returncode == 0, out.stderr
    first, last = out.stdout.split()
    assert first == last


def test_py_keeps_an_existing_pythonpath_entry():
    out = _bash(f'PYTHONPATH=/extra; source "{SCRIPTS}/lib.sh"; HOMELAB_PYTHON=/usr/bin/true; py; py; echo "$PYTHONPATH"')
    entries = out.stdout.strip().split(":")
    assert entries.count("/extra") == 1
    assert len(entries) == 3


# 3. supervise.sh restarts a command that exits, and stops cleanly ----------

def test_supervise_restarts_until_the_limit(tmp_path):
    marker = tmp_path / "runs"
    out = subprocess.run(
        [str(SCRIPTS / "supervise.sh"), "demo", "sh", "-c", f"echo run >> {marker}; exit 3"],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "HOMELAB_SUPERVISE_MAX_RESTARTS": "2", "HOMELAB_SUPERVISE_BACKOFF_START": "0"},
    )
    assert marker.read_text().count("run") == 3
    assert out.returncode == 3
    assert "restart limit reached" in out.stderr


def test_kill_tree_stops_supervisor_and_child(tmp_path):
    out = _bash(
        f'export HOMELAB_DATA="{tmp_path}"; source "{SCRIPTS}/lib.sh"; '
        f'"{SCRIPTS}/supervise.sh" sleeper sleep 300 & sup=$!; sleep 1; '
        'child="$(ps -o pid= --ppid "$sup" | tr -d " " | head -1)"; '
        'kill_tree "$sup"; sleep 1; '
        'for p in "$sup" "$child"; do kill -0 "$p" 2>/dev/null && echo "alive $p"; done; '
        'echo "child=$child"',
        env={"HOMELAB_SUPERVISE_BACKOFF_START": "0"},
    )
    assert "child=" in out.stdout and "child=\n" not in out.stdout, out.stdout
    assert "alive" not in out.stdout, out.stdout


# 1b. score loop: an empty ingest buffer is a quiet window, not an error ----

class _Handler(http.server.BaseHTTPRequestHandler):
    status = 400
    body = b'{"detail":"Ingest buffer is empty"}'

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):
        pass


@pytest.mark.parametrize(
    ("status", "body", "expect_out", "expect_err"),
    [
        (400, b'{"detail":"Ingest buffer is empty"}', "no new alerts since the last job", ""),
        (200, b'{"job_id":"abc","shadow_mode":true}', 'accepted {"job_id":"abc"', ""),
        (400, b'{"detail":"something else"}', "", "did not accept the shadow job (HTTP 400)"),
    ],
)
def test_score_loop_reports_each_response(tmp_path, status, body, expect_out, expect_err):
    handler = type("H", (_Handler,), {"status": status, "body": body})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        (tmp_path / "endpoints.env").write_text(f"APP_URL=http://127.0.0.1:{server.server_port}\n")
        out = subprocess.run(
            [str(SCRIPTS / "score-loop.sh"), "--once"], capture_output=True, text=True, timeout=30,
            env={**os.environ, "HOMELAB_DATA": str(tmp_path)},
        )
    finally:
        server.shutdown()
    assert out.returncode == 0
    assert expect_out in out.stdout
    assert expect_err in out.stderr


# 2. scheduler: kubectl timeouts and finalizer clearing ---------------------

class _Co:
    id = "borealis"
    namespace = "company-borealis"


def test_kubectl_timeout_is_reported_not_raised(monkeypatch):
    from homelab import scheduler

    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout"))

    monkeypatch.setattr(scheduler.subprocess, "run", boom)
    out = scheduler._run(["delete", "x"], timeout=1)
    assert out.returncode == 124


def test_stuck_delete_clears_finalizers(monkeypatch):
    from homelab import scheduler

    calls: list[list[str]] = []
    state = {"present": True}

    def fake_run(args, input_text=None, timeout=None):
        calls.append(args)
        if "patch" in args:
            state["present"] = False
        if "get" in args:
            stdout = "httpchaos.chaos-mesh.org/homelab\n" if state["present"] else ""
            return subprocess.CompletedProcess(args, 0, stdout, "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(scheduler, "_run", fake_run)
    outcome = scheduler.delete_experiment(_Co(), dry_run=False, wait_seconds=0, sleep=lambda _s: None)
    assert outcome == "cleared"
    delete = next(c for c in calls if "delete" in c)
    assert "--wait=false" in delete
    patch = next(c for c in calls if "patch" in c)
    assert "httpchaos.chaos-mesh.org/homelab" in patch
    assert scheduler.CLEAR_FINALIZERS in patch


def test_never_injected_clears_finalizers_without_waiting(monkeypatch):
    from homelab import scheduler

    calls: list[list[str]] = []
    state = {"present": True}

    def fake_run(args, input_text=None, timeout=None):
        calls.append(args)
        if "patch" in args:
            state["present"] = False
        if "get" in args:
            return subprocess.CompletedProcess(args, 0, "httpchaos.chaos-mesh.org/homelab\n" if state["present"] else "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(scheduler, "_run", fake_run)
    slept: list[float] = []
    outcome = scheduler.delete_experiment(_Co(), dry_run=False, clear_now=True, wait_seconds=600, sleep=slept.append)
    assert outcome == "cleared"
    assert slept == []


def test_delete_that_never_goes_away_is_stuck(monkeypatch):
    from homelab import scheduler

    def fake_run(args, input_text=None, timeout=None):
        if "get" in args:
            return subprocess.CompletedProcess(args, 0, "httpchaos.chaos-mesh.org/homelab\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(scheduler, "_run", fake_run)
    assert scheduler.delete_experiment(_Co(), dry_run=False, wait_seconds=0, sleep=lambda _s: None) == "stuck"


@pytest.mark.parametrize(
    ("counts", "outcome", "status"),
    [
        ((0, 0), "cleared", "not-injected"),
        ((1, 1), "gone", "closed"),
        ((1, 1), "cleared", "closed"),
        ((1, 0), "cleared", "unrecovered"),
        ((1, 0), "gone", "closed"),
        (None, "gone", "closed"),
    ],
)
def test_close_status(counts, outcome, status):
    from homelab.scheduler import close_status

    assert close_status(counts, outcome) == status


def test_step_company_marks_never_injected_and_closes(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from homelab import scheduler

    start = datetime(2026, 10, 22, 15, 0, tzinfo=timezone.utc)
    row = {"fault_id": "f1", "fault_type": "http-abort", "service": "checkout", "status": "running",
           "end": (start + timedelta(minutes=3)).isoformat().replace("+00:00", "Z")}
    state = json.loads(json.dumps({"faults": [row], "companies": {"borealis": {"open": row, "next_start": "2026-10-23T00:00:00Z"}}}))
    monkeypatch.setattr(scheduler, "experiment_counts", lambda company, fault_type: (0, 0))
    seen = {}

    def fake_delete(company, dry_run, clear_now=False, **kwargs):
        seen["clear_now"] = clear_now
        return "cleared"

    monkeypatch.setattr(scheduler, "delete_experiment", fake_delete)
    assert scheduler.step_company(state, _Co(), start + timedelta(minutes=4), dry_run=False) == "closed"
    assert seen["clear_now"] is True
    assert state["faults"][0]["status"] == "not-injected"
    assert state["companies"]["borealis"]["open"] is None


# 4. helm: skip an unchanged release ---------------------------------------

def _runner(listed, values):
    def run(args):
        if args[0] == "list":
            return subprocess.CompletedProcess(args, 0, json.dumps(listed), "")
        return subprocess.CompletedProcess(args, 0, json.dumps(values), "")

    return run


def test_helm_skips_unchanged_release():
    from homelab.helmstate import decide

    listed = [{"name": "chaos-mesh", "status": "deployed", "chart": "chaos-mesh-2.7.1"}]
    values = {"chaosDaemon": {"runtime": "containerd"}}
    assert decide("chaos-mesh", "chaos-mesh", "2.7.1", values, _runner(listed, values))[0] == "skip"


@pytest.mark.parametrize(
    ("listed", "deployed", "reason"),
    [
        ([], {}, "not installed"),
        ([{"name": "chaos-mesh", "status": "failed", "chart": "chaos-mesh-2.7.1"}], {"a": 1}, "status is failed"),
        ([{"name": "chaos-mesh", "status": "deployed", "chart": "chaos-mesh-2.7.0"}], {"a": 1}, "!= 2.7.1"),
        ([{"name": "chaos-mesh", "status": "deployed", "chart": "chaos-mesh-2.7.1"}], {"a": 2}, "values changed"),
    ],
)
def test_helm_upgrades_when_something_differs(listed, deployed, reason):
    from homelab.helmstate import decide

    verdict, why = decide("chaos-mesh", "chaos-mesh", "2.7.1", {"a": 1}, _runner(listed, deployed))
    assert verdict == "upgrade"
    assert reason in why


def test_wanted_values_applies_set_overrides(tmp_path):
    from homelab.helmstate import wanted_values

    values = tmp_path / "v.yaml"
    values.write_text("chaosDaemon:\n  runtime: containerd\n")
    got = wanted_values(values, ["chaosDaemon.socketPath=/run/x.sock"])
    assert got == {"chaosDaemon": {"runtime": "containerd", "socketPath": "/run/x.sock"}}


# collect loop: Prometheus 11,000-point limit --------------------------------

def test_collect_loop_clamps_the_range():
    text = (SCRIPTS / "collect-loop.sh").read_text()
    assert 'oldest="$((now - 7 * 24 * 3600))"' in text
    # 7 days at step=60s stays under Prometheus' 11,000 points per series.
    assert 7 * 24 * 60 < 11000
