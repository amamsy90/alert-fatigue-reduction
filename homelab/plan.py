"""Render the manifests the shell applies. Rendering does not talk to a cluster."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .catalog import OTEL_DISABLED
from .companies import Company, load_companies, select_companies
from .rules import render_all
from .safety import WEBHOOK_URL, app_manifest
from .versions import load_versions

OTEL_CHART = "open-telemetry/opentelemetry-demo"


def boutique_url(versions: dict[str, str] | None = None) -> str:
    ref = (versions or load_versions())["BOUTIQUE_REF"]
    return (
        "https://raw.githubusercontent.com/GoogleCloudPlatform/microservices-demo/"
        f"{ref}/release/kubernetes-manifests.yaml"
    )


def sock_shop_url(versions: dict[str, str] | None = None) -> str:
    ref = (versions or load_versions())["SOCK_SHOP_REF"]
    return (
        "https://raw.githubusercontent.com/microservices-demo/microservices-demo/"
        f"{ref}/deploy/kubernetes/complete-demo.yaml"
    )


def rewrite_namespace(text: str, source: str, target: str) -> str:
    return text.replace(f"namespace: {source}", f"namespace: {target}").replace(
        f"name: {source}\n", f"name: {target}\n"
    )


def otel_demo_values(company: Company) -> str:
    """Helm values for one reduced OpenTelemetry demo.

    Kafka, accounting, and fraud-detection are off. Checkout's chart default
    still waits for ``kafka:9092`` and then panics on the first order if the
    producer is nil, so these values drop that init container and set
    ``KAFKA_ADDR`` empty. The 3.1.0 checkout binary skips the order bus when
    that variable is empty and still serves cart, payment, shipping, and email.
    """
    disabled = "\n".join(
        f"  {name}:\n    enabled: false" for name in OTEL_DISABLED
    )
    return f"""\
default:
  envOverrides:
    - name: OTEL_COLLECTOR_NAME
      value: otel-collector.observability.svc.cluster.local
    - name: OTEL_RESOURCE_ATTRIBUTES_EXTRA
      value: company={company.id}
opentelemetry-collector:
  enabled: false
otel-ebpf-profiler:
  enabled: false
jaeger:
  enabled: false
prometheus:
  enabled: false
grafana:
  enabled: false
opensearch:
  enabled: false
components:
{disabled}
  ad:
    # The Java ad service is OOMKilled at the chart default of 300Mi.
    resources:
      limits:
        memory: 600Mi
  checkout:
    initContainers: []
    envOverrides:
      - name: KAFKA_ADDR
        value: ""
"""


def loadgenerator_manifest(company: Company, image: str | None = None) -> str:
    ns = company.namespace
    curl = image or load_versions()["CURL_IMAGE"]
    return f"""\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: loadgenerator
  namespace: {ns}
  labels:
    app: loadgenerator
    company: {company.id}
    homelab-load: "yes"
spec:
  replicas: 1
  selector:
    matchLabels:
      app: loadgenerator
  template:
    metadata:
      labels:
        app: loadgenerator
        company: {company.id}
        service: loadgenerator
        homelab-load: "yes"
    spec:
      containers:
        - name: load
          image: {curl}
          command: ["/bin/sh", "-c"]
          args:
            - |
              while true; do
                curl -sS -o /dev/null -m 2 http://front-end.{ns}.svc.cluster.local/ || true
                sleep 0.2
              done
          resources:
            requests:
              cpu: 20m
              memory: 32Mi
            limits:
              memory: 128Mi
"""


def match_deployment(names: list[str], service: str) -> str | None:
    """Deployment whose name is `service` or ends with `-service`."""
    if service in names:
        return service
    hits = [name for name in names if name.endswith("-" + service)]
    if len(hits) == 1:
        return hits[0]
    return None


def service_for_deployment(deploy_name: str, catalog: list[str]) -> str | None:
    """Catalog service for a Deployment, including a Helm release prefix.

    The longest catalog name wins, so `product-catalog` beats a shorter suffix.
    """
    if deploy_name in catalog:
        return deploy_name
    hits = [name for name in catalog if deploy_name.endswith("-" + name)]
    if not hits:
        return None
    hits.sort(key=len, reverse=True)
    return hits[0]


def steps_for(companies: list[Company], versions: dict[str, str] | None = None) -> list[dict[str, str]]:
    pins = versions or load_versions()
    steps: list[dict[str, str]] = []
    for company in companies:
        if company.app == "online-boutique":
            steps.append({
                "action": "kubectl-apply-url",
                "namespace": company.namespace,
                "url": boutique_url(pins),
                "company": company.id,
            })
        elif company.app == "otel-demo":
            steps.append(
                {
                    "action": "helm",
                    "namespace": company.namespace,
                    "release": company.id,
                    "chart": OTEL_CHART,
                    "version": pins["OTEL_DEMO_CHART_VERSION"],
                    "company": company.id,
                }
            )
        elif company.app == "sock-shop":
            steps.append(
                {
                    "action": "kubectl-apply-url",
                    "namespace": company.namespace,
                    "url": sock_shop_url(pins),
                    "rewrite_from": "sock-shop",
                    "company": company.id,
                }
            )
        elif company.app == "manifest":
            steps.append(
                {
                    "action": "kubectl-apply-url",
                    "namespace": company.namespace,
                    "url": company.manifest_url,
                    "company": company.id,
                }
            )
        else:
            raise ValueError(f"no deploy step for {company.app}")
    return steps


def render(companies: list[Company], dest: Path) -> dict:
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "oncall-queue.yaml").write_text(app_manifest(), encoding="utf-8")
    (dest / "rules.yaml").write_text(render_all(companies), encoding="utf-8")
    for company in companies:
        if company.app == "otel-demo":
            (dest / f"otel-demo-{company.id}.yaml").write_text(otel_demo_values(company), encoding="utf-8")
        if company.app == "sock-shop":
            (dest / f"loadgenerator-{company.id}.yaml").write_text(
                loadgenerator_manifest(company), encoding="utf-8"
            )
    plan = {"webhook": WEBHOOK_URL, "companies": [company.id for company in companies], "steps": steps_for(companies)}
    (dest / "plan.json").write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    return plan


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Render homelab manifests. Does not start a cluster.")
    parser.add_argument("--companies-file", type=Path, default=None)
    parser.add_argument("--companies", default="", help="Comma-separated ids. Default: every enabled company.")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--tsv", action="store_true", help="Print id, app and namespace, one company per line.")
    parser.add_argument("--rewrite-from", default="", help="Read stdin, rewrite a namespace, write stdout.")
    parser.add_argument("--rewrite-to", default="")
    args = parser.parse_args(argv)
    if args.rewrite_from:
        if not args.rewrite_to:
            raise SystemExit("--rewrite-to is required with --rewrite-from")
        sys.stdout.write(rewrite_namespace(sys.stdin.read(), args.rewrite_from, args.rewrite_to))
        return
    selected = select_companies(load_companies(args.companies_file), args.companies or None)
    if args.tsv:
        for company in selected:
            print(f"{company.id}\t{company.app}\t{company.namespace}")
        return
    if args.out is None:
        raise SystemExit("--out is required when rendering")
    render(selected, args.out)
    print(f"rendered {len(selected)} companies into {args.out}")


if __name__ == "__main__":
    main()
