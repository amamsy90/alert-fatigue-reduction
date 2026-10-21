"""Shadow-only settings for the lab's copy of the app.

Nothing here turns Live on, sets a paging key, or sets ALLOW_OPEN_ACCESS.
The product defaults are unchanged. This module only checks the lab manifest.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

FORBIDDEN_SNIPPETS = (
    "SHADOW_MODE\n          value: \"0\"",
    "ALLOW_OPEN_ACCESS\n          value: \"1\"",
    "LLM_EXPLAIN\n          value: \"1\"",
    "LEARNED_RANKER\n          value: \"1\"",
    "METRIC_MODEL\n          value: \"1\"",
    "PAGERDUTY_ROUTING_KEY",
    "SLACK_WEBHOOK_URL",
    "OPSGENIE_API_KEY",
    "APP_PASSWORD",
    "score_now",
    "confirm: LIVE",
    "ALLOW_OPEN_ACCESS: \"1\"",
    "SHADOW_MODE: \"0\"",
)

WEBHOOK_URL = "http://oncall-queue.observability.svc.cluster.local:8080/ingest/alertmanager"


def app_manifest() -> str:
    """In-cluster Deployment. Live stays impossible: no password and no channel."""
    return """\
apiVersion: v1
kind: Namespace
metadata:
  name: observability
---
apiVersion: v1
kind: Service
metadata:
  name: oncall-queue
  namespace: observability
  labels:
    app: oncall-queue
spec:
  selector:
    app: oncall-queue
  ports:
    - name: http
      port: 8080
      targetPort: http
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: oncall-queue-data
  namespace: observability
spec:
  accessModes: ["ReadWriteOnce"]
  storageClassName: local-path
  resources:
    requests:
      storage: 2Gi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: oncall-queue
  namespace: observability
  labels:
    app: oncall-queue
spec:
  replicas: 1
  selector:
    matchLabels:
      app: oncall-queue
  template:
    metadata:
      labels:
        app: oncall-queue
    spec:
      containers:
        - name: app
          image: oncall-queue:homelab
          imagePullPolicy: Never
          ports:
            - name: http
              containerPort: 8080
          env:
            - name: SHADOW_MODE
              value: "1"
            - name: ALLOW_OPEN_ACCESS
              value: "0"
            - name: LLM_EXPLAIN
              value: "0"
            - name: LEARNED_RANKER
              value: "0"
            - name: METRIC_MODEL
              value: "0"
            - name: QUEUE_DB
              value: /app/product/data/queue.db
            - name: PROM_ALLOW_PRIVATE
              value: "0"
          readinessProbe:
            httpGet:
              path: /health
              port: http
            initialDelaySeconds: 5
            periodSeconds: 10
          resources:
            requests:
              cpu: 100m
              memory: 256Mi
            limits:
              memory: 1Gi
          volumeMounts:
            - name: data
              mountPath: /app/product/data
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: oncall-queue-data
"""


def assert_shadow_manifest(text: str) -> None:
    for snippet in FORBIDDEN_SNIPPETS:
        if snippet in text:
            raise ValueError(f"lab manifest must not contain {snippet!r}")
    required = (
        'name: SHADOW_MODE\n              value: "1"',
        'name: ALLOW_OPEN_ACCESS\n              value: "0"',
        'name: LLM_EXPLAIN\n              value: "0"',
        'name: LEARNED_RANKER\n              value: "0"',
        'name: METRIC_MODEL\n              value: "0"',
        'name: PROM_ALLOW_PRIVATE\n              value: "0"',
    )
    for snippet in required:
        if snippet not in text:
            raise ValueError(f"lab manifest is missing {snippet!r}")
    if "0.0.0.0" in text:
        raise ValueError("lab manifest must not publish the app on every interface")


def assert_shadow_status(payload: dict) -> None:
    """Refuse to treat the app as the lab sink unless it cannot page."""
    if not isinstance(payload, dict):
        raise ValueError("connectors payload must be an object")
    if payload.get("shadow_mode") is not True or payload.get("live") is not False:
        raise ValueError("app is not in shadow mode; the lab will not send it alerts")
    pager = payload.get("pagerduty") or {}
    slack = payload.get("slack") or {}
    opsgenie = payload.get("opsgenie") or {}
    if pager.get("configured") or pager.get("will_page"):
        raise ValueError("PagerDuty is configured; the lab refuses to attach")
    if slack.get("configured") or slack.get("will_page"):
        raise ValueError("Slack is configured; the lab refuses to attach")
    if opsgenie.get("configured") or opsgenie.get("will_page"):
        raise ValueError("Opsgenie is configured; the lab refuses to attach")
    if payload.get("login_required"):
        raise ValueError("APP_PASSWORD is set on this instance; use a fresh lab volume")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Check the lab app stays in shadow and cannot page.")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--connectors", type=Path)
    args = parser.parse_args(argv)
    if args.manifest:
        assert_shadow_manifest(args.manifest.read_text(encoding="utf-8"))
        print("manifest ok")
    if args.connectors:
        assert_shadow_status(json.loads(args.connectors.read_text(encoding="utf-8")))
        print("connectors ok")
    if args.manifest is None and args.connectors is None:
        assert_shadow_manifest(app_manifest())
        print("built-in manifest ok")


if __name__ == "__main__":
    main()
