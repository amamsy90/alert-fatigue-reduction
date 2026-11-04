"""Pod-template labels and Online Boutique tracing env.

Chaos Mesh selects pods, so the labels have to land on the pod template.
`kubectl label` on the Deployment object does not. The load deployment is
marked `homelab-load=yes` so the scheduler can scale it.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from .catalog import App
from .companies import Company, load_companies, select_companies
from .plan import service_for_deployment

COLLECTOR_HOST = "otel-collector.observability.svc.cluster.local"


def _is_load(name: str, app: App) -> bool:
    suffix = app.load_deployment_suffix
    return name == suffix or name.endswith("-" + suffix)


def tracing_env(company: Company) -> list[tuple[str, str]]:
    return [
        ("ENABLE_TRACING", "1"),
        ("COLLECTOR_SERVICE_ADDR", f"{COLLECTOR_HOST}:4317"),
        ("OTEL_EXPORTER_OTLP_ENDPOINT", f"http://{COLLECTOR_HOST}:4317"),
        ("OTEL_RESOURCE_ATTRIBUTES", f"company={company.id}"),
    ]


def actions_for(company: Company, deploy_names: list[str]) -> list[dict]:
    """One action per deployment the lab knows how to label."""
    app = company.application()
    if app is None:
        return []
    actions: list[dict] = []
    catalog = app.names()
    for name in deploy_names:
        service = service_for_deployment(name, catalog)
        load = _is_load(name, app)
        if service is None and not load:
            continue
        pod_labels = {"company": company.id, "homelab": "yes"}
        if service:
            pod_labels["service"] = service
        if load:
            pod_labels["homelab-load"] = "yes"
        patch = {"spec": {"template": {"metadata": {"labels": pod_labels}}}}
        action: dict = {
            "op": "patch",
            "namespace": company.namespace,
            "deployment": name,
            "service": service or "",
            "load": load,
            "patch": patch,
        }
        if load:
            action["deploy_label"] = "homelab-load=yes"
        if app.tracing == "boutique" and not load:
            action["env"] = tracing_env(company)
        actions.append(action)
    return actions


def _kubectl(args: list[str], dry_run: bool) -> None:
    if dry_run:
        print("kubectl " + " ".join(args))
        return
    subprocess.run(["kubectl", *args], check=True)


def apply_actions(actions: list[dict], dry_run: bool) -> None:
    for action in actions:
        namespace = action["namespace"]
        deployment = action["deployment"]
        _kubectl(
            ["-n", namespace, "patch", "deploy", deployment, "--type", "merge", "-p", json.dumps(action["patch"])],
            dry_run,
        )
        if action.get("deploy_label"):
            _kubectl(
                ["-n", namespace, "label", "deploy", deployment, action["deploy_label"], "--overwrite"],
                dry_run,
            )
        env = action.get("env") or []
        if env:
            pairs = [f"{key}={value}" for key, value in env]
            _kubectl(["-n", namespace, "set", "env", f"deploy/{deployment}", *pairs], dry_run)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Label company workloads. Pass --dry-run to print kubectl only.")
    parser.add_argument("--companies-file", type=Path, default=None)
    parser.add_argument("--company", required=True)
    parser.add_argument("--names", required=True, help="Comma-separated Deployment names.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--print", action="store_true", help="Print the action JSON and do not call kubectl.")
    args = parser.parse_args(argv)
    chosen = [company for company in load_companies(args.companies_file) if company.id == args.company]
    if not chosen:
        known = select_companies(load_companies(args.companies_file), None)
        raise SystemExit(f"unknown company {args.company}; enabled: {', '.join(company.id for company in known)}")
    names = [part.strip() for part in args.names.split(",") if part.strip()]
    actions = actions_for(chosen[0], names)
    if args.print:
        print(json.dumps(actions, indent=2))
        return
    apply_actions(actions, args.dry_run)


if __name__ == "__main__":
    main()
