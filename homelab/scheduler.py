"""Run one Chaos Mesh fault at a time per company, and record ground truth.

The loop sleeps until the next start, so a laptop that wakes later simply
closes an overdue fault and waits out whatever gap is left. State is a JSON
file. The CSV is rewritten from that state.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .chaos import EXPERIMENT_NAME, FAULT_COLUMNS, Choice, FaultRow, choose, fault_id, gap_minutes, manifest
from .companies import Company, load_companies, select_companies
from .loadprof import replicas_for

KINDS = "podchaos,networkchaos,stresschaos,httpchaos"


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def load_state(path: Path, companies: list[Company]) -> dict:
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
    else:
        state = {"faults": [], "companies": {}}
    for company in companies:
        state["companies"].setdefault(
            company.id,
            {"seed": random.SystemRandom().randrange(1, 2**31), "next_start": utcnow().isoformat(), "open": None, "burst_until": None},
        )
    return state


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def write_faults(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(FAULT_COLUMNS))
        writer.writeheader()
        for row in state["faults"]:
            writer.writerow(row)


def _rng(state: dict, company: str) -> random.Random:
    slot = state["companies"][company]
    rng = random.Random(slot["seed"])
    raw = slot.get("rng")
    if raw:
        version, inner, gauss = raw
        rng.setstate((version, tuple(inner), gauss))
    return rng


def _store_rng(state: dict, company: str, rng: random.Random) -> None:
    version, inner, gauss = rng.getstate()
    state["companies"][company]["rng"] = [version, list(inner), gauss]


def close_open(state: dict, company: Company, now: datetime, status: str) -> dict | None:
    slot = state["companies"][company.id]
    open_row = slot.get("open")
    if not open_row:
        return None
    end = _parse(open_row["end"])
    if status in ("not-injected", "unrecovered"):
        open_row["status"] = status
        open_row["description"] = f"{open_row['fault_type']} on {open_row['service']} ({status.replace('-', ' ')})"
        if now < end:
            open_row["end"] = now.isoformat().replace("+00:00", "Z")
    elif status == "truncated":
        open_row["end"] = now.isoformat().replace("+00:00", "Z")
        open_row["status"] = "truncated"
        open_row["description"] = f"{open_row['fault_type']} on {open_row['service']} (truncated)"
    else:
        open_row["status"] = "closed"
        open_row["description"] = f"{open_row['fault_type']} on {open_row['service']} (closed)"
        if now < end:
            open_row["end"] = now.isoformat().replace("+00:00", "Z")
    slot["open"] = None
    # After a restart the open row and its entry in state["faults"] are separate
    # copies loaded from JSON, so update the stored row by id as well.
    for row in state.get("faults", []):
        if row is not open_row and row.get("fault_id") == open_row.get("fault_id"):
            row.update(open_row)
    return open_row


def due(state: dict, company: Company, now: datetime) -> str:
    """What this company needs at `now`: wait, close, or start."""
    slot = state["companies"][company.id]
    if slot.get("open"):
        if now >= _parse(slot["open"]["end"]):
            return "close"
        return "wait"
    if now >= _parse(slot["next_start"]):
        return "start"
    return "wait"


def open_fault(state: dict, company: Company, choice: Choice, start: datetime) -> dict:
    row = FaultRow(
        fault_id=fault_id(company.id, choice.service, choice.fault_type, start),
        company=company.id,
        service=choice.service,
        fault_type=choice.fault_type,
        start=start,
        end=start + choice.recorded,
        status="running",
    ).as_dict()
    state["faults"].append(row)
    state["companies"][company.id]["open"] = row
    return row


def schedule_next(state: dict, company: Company, start: datetime, rng: random.Random) -> None:
    minutes = gap_minutes(company, rng)
    state["companies"][company.id]["next_start"] = (start + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
    _store_rng(state, company.id, rng)


# Every kubectl call has a hard timeout. Without one, a delete that waits on a
# Chaos Mesh finalizer blocked the scheduler for hours (Oct 21 15:01 CST).
KUBECTL_TIMEOUT = int(os.environ.get("HOMELAB_KUBECTL_TIMEOUT", "60"))
DELETE_WAIT_SECONDS = int(os.environ.get("HOMELAB_DELETE_WAIT", "90"))
CLEAR_FINALIZERS = '{"metadata":{"finalizers":[]}}'


def _run(args: list[str], input_text: str | None = None, timeout: int | None = None) -> subprocess.CompletedProcess:
    """Run kubectl with a timeout. A timeout is returned as exit code 124."""
    limit = KUBECTL_TIMEOUT if timeout is None else timeout
    try:
        out = subprocess.run(
            ["kubectl", *args], input=input_text, capture_output=True, text=True, timeout=limit, check=False
        )
    except subprocess.TimeoutExpired:
        print(f"kubectl {' '.join(args)} timed out after {limit}s", file=sys.stderr)
        return subprocess.CompletedProcess(["kubectl", *args], 124, "", "timeout")
    if out.stdout:
        print(out.stdout, end="" if out.stdout.endswith("\n") else "\n")
    if out.stderr:
        print(out.stderr, end="" if out.stderr.endswith("\n") else "\n", file=sys.stderr)
    return out


def kubectl(args: list[str], dry_run: bool, check: bool = True) -> None:
    if dry_run:
        print("kubectl " + " ".join(args))
        return
    out = _run(args)
    if check and out.returncode != 0:
        raise subprocess.CalledProcessError(out.returncode, ["kubectl", *args])


def apply_load(state: dict, company: Company, now: datetime, dry_run: bool) -> int:
    """Scale the company's load deployment. A missing label is a warning, not a crash."""
    app = company.application()
    if app is None:
        return 0
    slot = state["companies"][company.id]
    raw = slot.get("burst_until")
    burst = _parse(raw) if raw else None
    replicas, until = replicas_for(company.load_profile, now, burst)
    slot["burst_until"] = until.isoformat().replace("+00:00", "Z") if until else None
    kubectl(
        ["-n", company.namespace, "scale", "deploy", "-l", "homelab-load=yes", f"--replicas={replicas}"],
        dry_run,
        check=False,
    )
    return replicas


FAULT_KIND = {
    "pod-kill": "podchaos",
    "network-delay": "networkchaos",
    "network-loss": "networkchaos",
    "cpu-stress": "stresschaos",
    "memory-stress": "stresschaos",
    "http-abort": "httpchaos",
}


def experiment_counts(company: Company, fault_type: str) -> tuple[int, int] | None:
    """(injectedCount, recoveredCount) summed over records; None if unknown."""
    kind = FAULT_KIND.get(fault_type)
    if not kind:
        return None
    out = subprocess.CompletedProcess([], 1)
    try:
        out = subprocess.run(
            ["kubectl", "-n", company.namespace, "get", kind, EXPERIMENT_NAME, "-o",
             "jsonpath={.status.experiment.containerRecords[*].injectedCount}"
             "{'|'}{.status.experiment.containerRecords[*].recoveredCount}"],
            capture_output=True, text=True, check=False, timeout=KUBECTL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None
    if out.returncode != 0 or "|" not in (out.stdout or ""):
        return None
    injected, recovered = out.stdout.split("|", 1)
    return (
        sum(int(x) for x in injected.split() if x.isdigit()),
        sum(int(x) for x in recovered.split() if x.isdigit()),
    )


def injected_count(company: Company, fault_type: str) -> int | None:
    counts = experiment_counts(company, fault_type)
    return None if counts is None else counts[0]


def _remaining(company: Company) -> list[str]:
    out = _run(["-n", company.namespace, "get", KINDS, EXPERIMENT_NAME, "--ignore-not-found", "-o", "name"])
    if out.returncode != 0:
        return ["unknown"]
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def clear_finalizers(company: Company, names: list[str]) -> None:
    for name in names:
        if name == "unknown":
            continue
        _run(["-n", company.namespace, "patch", name, "--type=merge", "-p", CLEAR_FINALIZERS])


def delete_experiment(
    company: Company,
    dry_run: bool,
    clear_now: bool = False,
    wait_seconds: int | None = None,
    sleep=time.sleep,
) -> str:
    """Delete the experiment without blocking on Chaos Mesh's finalizer.

    Returns "gone", or "cleared" when the finalizer had to be removed by hand
    (immediately when clear_now, else after wait_seconds), or "stuck".
    """
    args = ["-n", company.namespace, "delete", KINDS, EXPERIMENT_NAME, "--ignore-not-found", "--wait=false"]
    if dry_run:
        print("kubectl " + " ".join(args))
        return "gone"
    _run(args)
    if clear_now:
        # Nothing was injected, so there is nothing for Chaos Mesh to recover.
        clear_finalizers(company, _remaining(company))
    limit = DELETE_WAIT_SECONDS if wait_seconds is None else wait_seconds
    deadline = time.monotonic() + limit
    while True:
        left = _remaining(company)
        if not left:
            return "cleared" if clear_now else "gone"
        if time.monotonic() >= deadline:
            break
        sleep(2)
    print(f"{company.id}: delete still waiting after {limit}s on {left}; clearing finalizers", file=sys.stderr)
    clear_finalizers(company, left)
    sleep(2)
    return "cleared" if not _remaining(company) else "stuck"


def apply_manifest(text: str, dry_run: bool) -> None:
    if dry_run:
        print(text)
        return
    out = _run(["apply", "-f", "-"], input_text=text)
    if out.returncode != 0:
        raise subprocess.CalledProcessError(out.returncode, ["kubectl", "apply", "-f", "-"])


def close_status(counts: tuple[int, int] | None, outcome: str) -> str:
    """Ground-truth status for a fault that is being closed.

    Only "closed" rows are scored by evaluate.py.
    """
    if counts is not None and counts[0] == 0:
        return "not-injected"
    if outcome in ("cleared", "stuck") and counts is not None and counts[1] < counts[0]:
        # Chaos Mesh never confirmed recovery, so the end time is not trustworthy.
        return "unrecovered"
    return "closed"


def step_company(state: dict, company: Company, now: datetime, dry_run: bool) -> str:
    action = due(state, company, now)
    if action == "wait":
        return "wait"
    if action == "close":
        open_row = state["companies"][company.id].get("open") or {}
        counts = None if dry_run else experiment_counts(company, open_row.get("fault_type", ""))
        never = counts is not None and counts[0] == 0
        if never:
            print(f"{company.id}: {open_row.get('fault_type')} was never injected", file=sys.stderr)
        outcome = delete_experiment(company, dry_run, clear_now=never)
        if outcome == "stuck":
            print(f"{company.id}: experiment still present after clearing finalizers; will retry", file=sys.stderr)
            return "error"
        close_open(state, company, now, close_status(counts, outcome))
        return "closed"
    app = company.application()
    if app is None:
        return "skip"
    rng = _rng(state, company.id)
    choice = choose(company, app, rng)
    if choice is None:
        schedule_next(state, company, now, rng)
        return "skip"
    text = manifest(company, choice, app)
    if delete_experiment(company, dry_run) == "stuck":
        print(f"{company.id}: an old experiment is still present; will retry", file=sys.stderr)
        return "error"
    try:
        apply_manifest(text, dry_run)
    except subprocess.CalledProcessError as exc:
        print(f"{company.id}: chaos apply failed ({exc.returncode}); will retry", file=sys.stderr)
        return "error"
    open_fault(state, company, choice, now)
    schedule_next(state, company, now, rng)
    return "started"


def run_loop(companies: list[Company], state_path: Path, faults_path: Path, dry_run: bool, once: bool) -> None:
    state = load_state(state_path, companies)
    for company in companies:
        if state["companies"][company.id].get("open") and due(state, company, utcnow()) == "close":
            step_company(state, company, utcnow(), dry_run)
    save_state(state_path, state)
    write_faults(faults_path, state)
    while True:
        now = utcnow()
        for company in companies:
            apply_load(state, company, now, dry_run)
            step_company(state, company, now, dry_run)
        save_state(state_path, state)
        write_faults(faults_path, state)
        if once or dry_run:
            return
        time.sleep(30)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Inject Chaos Mesh faults and write ground truth.")
    parser.add_argument("--companies-file", type=Path, default=None)
    parser.add_argument("--companies", default="")
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--faults", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    selected = select_companies(load_companies(args.companies_file), args.companies or None)
    run_loop(selected, args.state, args.faults, args.dry_run, args.once or args.dry_run)


if __name__ == "__main__":
    main()
