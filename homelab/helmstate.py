"""Decide whether up.sh needs to run `helm upgrade` for a release.

Re-running an unchanged chart is not free: the Chaos Mesh chart generates new
certificates and a random `rollme` annotation on every upgrade, so each upgrade
rolls the controllers. On Oct 22 one such upgrade was cancelled half way and
left the daemon and controllers with different certificates. up.sh therefore
skips a release whose chart version and user values already match.

usage: python -m homelab.helmstate --release R --namespace N --chart-version V
                                   --values FILE [--set k=v ...]
prints "skip" or "upgrade" (with a reason on stderr).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import yaml

Runner = Callable[[list[str]], subprocess.CompletedProcess]
HELM_TIMEOUT = 60


def _helm(args: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["helm", *args], capture_output=True, text=True, check=False, timeout=HELM_TIMEOUT)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(["helm", *args], 124, "", "timeout")


def _set_path(target: dict, dotted: str, value: object) -> None:
    keys = dotted.split(".")
    node = target
    for key in keys[:-1]:
        node = node.setdefault(key, {})
    node[keys[-1]] = value


def wanted_values(values_file: Path, sets: list[str]) -> dict:
    data = yaml.safe_load(values_file.read_text(encoding="utf-8")) or {}
    for item in sets:
        key, _, raw = item.partition("=")
        _set_path(data, key, yaml.safe_load(raw) if raw else raw)
    return data


def decide(
    release: str,
    namespace: str,
    chart_version: str,
    values: dict,
    run: Runner = _helm,
) -> tuple[str, str]:
    listed = run(["list", "-n", namespace, "--filter", f"^{release}$", "-o", "json"])
    if listed.returncode != 0:
        return "upgrade", f"helm list failed: {listed.stderr.strip()[:200]}"
    try:
        rows = json.loads(listed.stdout or "[]")
    except json.JSONDecodeError:
        return "upgrade", "helm list output was not JSON"
    row = next((r for r in rows if r.get("name") == release), None)
    if row is None:
        return "upgrade", "not installed"
    if row.get("status") != "deployed":
        return "upgrade", f"status is {row.get('status')}"
    chart = str(row.get("chart", ""))
    if not chart.endswith(f"-{chart_version}"):
        return "upgrade", f"chart {chart} != {chart_version}"
    current = run(["get", "values", release, "-n", namespace, "-o", "json"])
    if current.returncode != 0:
        return "upgrade", "helm get values failed"
    try:
        deployed = json.loads(current.stdout or "null") or {}
    except json.JSONDecodeError:
        return "upgrade", "helm get values output was not JSON"
    if deployed != values:
        return "upgrade", "values changed"
    return "skip", f"{release} {chart} deployed with the same values"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--release", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--chart-version", required=True)
    parser.add_argument("--values", type=Path, required=True)
    parser.add_argument("--set", action="append", default=[])
    args = parser.parse_args(argv)
    verdict, reason = decide(
        args.release, args.namespace, args.chart_version, wanted_values(args.values, args.set)
    )
    print(f"helm {args.release}: {verdict} ({reason})", file=sys.stderr)
    print(verdict)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
