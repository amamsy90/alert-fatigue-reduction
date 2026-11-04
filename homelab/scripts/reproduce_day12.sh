#!/usr/bin/env bash
# Verify the day-12 snapshot and compare a fresh score with the committed tables.
# Prints MATCH or MISMATCH for each table. Does not rewrite published numbers.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ARCHIVE="${ROOT}/homelab/snapshots/day12/day12-rebuilt-20261103T150116Z.tar.xz"
EXPECT="378dc71f3ef4143662acc1d0a3dda8c5e32f7752f3ceb77efe9b88bf0985c56e"
PUB="${ROOT}/docs/results/homelab_day12"
echo "${EXPECT}  ${ARCHIVE}" | sha256sum -c -
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
tar -xJf "${ARCHIVE}" -C "${WORK}"
export PYTHONPATH="${ROOT}:${ROOT}/app${PYTHONPATH:+:${PYTHONPATH}}"
py() {
  uv run --directory "${ROOT}/app" --with pyyaml python "$@"
}
py -m homelab.evaluate \
  --collected "${WORK}/data/collected" \
  --faults "${WORK}/data/faults.csv" \
  --out "${WORK}/out/plain"
py -m homelab.evaluate \
  --collected "${WORK}/data/collected" \
  --faults "${WORK}/data/faults.csv" \
  --learned-ranker \
  --out "${WORK}/out/ranker"
mkdir -p "${WORK}/out/boot" "${WORK}/out/feedback"
py "${PUB}/pooled_bootstrap.py" "${WORK}" "${WORK}/out/boot"
py "${PUB}/feedback_analysis.py" "${WORK}" "${WORK}/out/feedback"
cat > "${WORK}/compare.py" <<'PY'
import json
import sys
from pathlib import Path

import pandas as pd

pub = Path(sys.argv[1])
got = Path(sys.argv[2])
failed = False


def report(name, ok, detail=""):
    global failed
    print(f"{'MATCH' if ok else 'MISMATCH'} {name}" + (f" {detail}" if detail else ""))
    failed = failed or not ok


def as_num(value):
    try:
        if pd.isna(value):
            return None
        return round(float(value), 4)
    except (TypeError, ValueError):
        return str(value)


def cmp_csv(name, left, right, keys, cols):
    if not left.exists() or not right.exists():
        report(name, False, f"missing {left.name if not left.exists() else right.name}")
        return
    a = pd.read_csv(left)
    b = pd.read_csv(right)
    use = [c for c in cols if c in a.columns and c in b.columns]
    a = a.sort_values(keys).reset_index(drop=True)
    b = b.sort_values(keys).reset_index(drop=True)
    if len(a) != len(b):
        report(name, False, f"rows {len(a)} vs {len(b)}")
        return
    bad = []
    for i in range(len(a)):
        for col in use:
            va, vb = as_num(a.loc[i, col]), as_num(b.loc[i, col])
            if va != vb:
                bad.append(f"{col} {a.loc[i, keys[0]]}: {va} vs {vb}")
                if len(bad) >= 8:
                    break
        if len(bad) >= 8:
            break
    report(name, not bad, "; ".join(bad[:8]))


QUEUE_COLS = [
    "queue", "budget_k", "faults", "faults_caught", "detection_rate",
    "items_per_day", "false_items_per_day", "precision", "attached_alerts",
]
for label, rel in (
    ("plain pooled queue_eval", "plain/pooled/queue_eval.csv"),
    ("plain acme queue_eval", "plain/per_company/acme/queue_eval.csv"),
    ("plain cassini queue_eval", "plain/per_company/cassini/queue_eval.csv"),
    ("ranker pooled queue_eval", "ranker/pooled/queue_eval.csv"),
    ("ranker acme queue_eval", "ranker/per_company/acme/queue_eval.csv"),
    ("ranker cassini queue_eval", "ranker/per_company/cassini/queue_eval.csv"),
):
    cmp_csv(label, pub / rel, got / rel, ["queue", "budget_k"], QUEUE_COLS)

MISS = ["queue", "budget_k", "no_metric_data", "no_jump", "below_threshold", "budget_full"]
for label, rel in (
    ("plain pooled miss_causes", "plain/pooled/miss_causes.csv"),
    ("ranker pooled miss_causes", "ranker/pooled/miss_causes.csv"),
):
    cmp_csv(label, pub / rel, got / rel, ["queue", "budget_k"], MISS)

BOOT = [
    "population", "k", "baseline", "candidate_caught", "baseline_caught",
    "decision", "low", "high", "mean",
]
cmp_csv("pooled_bootstrap", pub / "pooled_bootstrap.csv", got / "boot/pooled_bootstrap.csv",
        ["population", "k", "baseline"], BOOT)
cmp_csv("feedback_bootstrap", pub / "feedback_bootstrap.csv", got / "feedback/feedback_bootstrap.csv",
        ["population", "k", "baseline"], BOOT)


def loco_rows(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    companies = data.get("companies")
    if not companies:
        return {"status": data.get("status"), "reason": data.get("reason"), "flags": data.get("product_flags")}
    rows = []
    for name, entry in companies.items():
        if not isinstance(entry, dict):
            continue
        base = entry.get("baseline") or {}
        learned = entry.get("learned_ranker") or {}
        delta = entry.get("per_fault_catch_delta") or {}
        rows.append({
            "company": name,
            "status": entry.get("status"),
            "decision": entry.get("decision"),
            "baseline_caught": base.get("faults_caught"),
            "ranker_caught": learned.get("faults_caught"),
            "low": None if delta.get("low") is None else round(float(delta["low"]), 4),
            "high": None if delta.get("high") is None else round(float(delta["high"]), 4),
        })
    return rows


for label, rel in (("plain leave_one_company_out", "plain/leave_one_company_out.json"),
                   ("ranker leave_one_company_out", "ranker/leave_one_company_out.json")):
    left, right = pub / rel, got / rel
    if not right.exists():
        report(label, False, "missing output")
        continue
    a, b = loco_rows(left), loco_rows(right)
    report(label, a == b, "" if a == b else f"{a} vs {b}")

sys.exit(1 if failed else 0)
PY
py "${WORK}/compare.py" "${PUB}" "${WORK}/out"
