"""Observability manifests rendered from companies.yaml and versions.env.

Loki, Tempo, Alloy and the collector are applied with kubectl. Prometheus
and Alertmanager come from the kube-prometheus-stack chart. Rendering does
not talk to a cluster.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from .companies import Company, load_companies, select_companies
from .versions import load_versions

NAMESPACES = """\
apiVersion: v1
kind: Namespace
metadata:
  name: observability
---
apiVersion: v1
kind: Namespace
metadata:
  name: chaos-mesh
"""


def _indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line if line else "" for line in text.splitlines())


def loki_manifest(image: str) -> str:
    config = """\
auth_enabled: false
server:
  http_listen_port: 3100
  grpc_listen_port: 9096
  log_level: info
common:
  instance_addr: 127.0.0.1
  path_prefix: /loki
  storage:
    filesystem:
      chunks_directory: /loki/chunks
      rules_directory: /loki/rules
  replication_factor: 1
  ring:
    kvstore:
      store: inmemory
schema_config:
  configs:
    - from: 2024-01-01
      store: tsdb
      object_store: filesystem
      schema: v13
      index:
        prefix: index_
        period: 24h
limits_config:
  retention_period: 72h
  allow_structured_metadata: false
compactor:
  working_directory: /loki/compactor
  compaction_interval: 10m
  retention_enabled: true
  retention_delete_delay: 2h
  delete_request_store: filesystem
analytics:
  reporting_enabled: false
"""
    return f"""\
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: loki
  namespace: observability
spec:
  accessModes: ["ReadWriteOnce"]
  storageClassName: local-path
  resources:
    requests:
      storage: 10Gi
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: loki
  namespace: observability
data:
  loki.yaml: |
{_indent(config, 4)}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: loki
  namespace: observability
  labels:
    app: loki
spec:
  replicas: 1
  selector:
    matchLabels:
      app: loki
  template:
    metadata:
      labels:
        app: loki
    spec:
      securityContext:
        fsGroup: 10001
      containers:
        - name: loki
          image: {image}
          args: ["-config.file=/etc/loki/loki.yaml"]
          ports:
            - name: http
              containerPort: 3100
          readinessProbe:
            httpGet:
              path: /ready
              port: http
            initialDelaySeconds: 10
            periodSeconds: 10
          resources:
            requests:
              cpu: 100m
              memory: 256Mi
            limits:
              memory: 1Gi
          volumeMounts:
            - name: config
              mountPath: /etc/loki
            - name: data
              mountPath: /loki
      volumes:
        - name: config
          configMap:
            name: loki
        - name: data
          persistentVolumeClaim:
            claimName: loki
---
apiVersion: v1
kind: Service
metadata:
  name: loki
  namespace: observability
  labels:
    app: loki
spec:
  selector:
    app: loki
  ports:
    - name: http
      port: 3100
      targetPort: http
"""


def tempo_manifest(image: str) -> str:
    # Tempo 2.7 listens on localhost unless the endpoint is 0.0.0.0.
    config = """\
server:
  http_listen_port: 3200
distributor:
  receivers:
    otlp:
      protocols:
        grpc:
          endpoint: 0.0.0.0:4317
        http:
          endpoint: 0.0.0.0:4318
ingester:
  max_block_duration: 30m
compactor:
  compaction:
    block_retention: 48h
storage:
  trace:
    backend: local
    wal:
      path: /var/tempo/wal
    local:
      path: /var/tempo/blocks
"""
    return f"""\
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: tempo
  namespace: observability
spec:
  accessModes: ["ReadWriteOnce"]
  storageClassName: local-path
  resources:
    requests:
      storage: 15Gi
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: tempo
  namespace: observability
data:
  tempo.yaml: |
{_indent(config, 4)}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: tempo
  namespace: observability
  labels:
    app: tempo
spec:
  replicas: 1
  selector:
    matchLabels:
      app: tempo
  template:
    metadata:
      labels:
        app: tempo
    spec:
      securityContext:
        fsGroup: 10001
      containers:
        - name: tempo
          image: {image}
          args: ["-config.file=/etc/tempo/tempo.yaml"]
          ports:
            - name: http
              containerPort: 3200
            - name: otlp-grpc
              containerPort: 4317
            - name: otlp-http
              containerPort: 4318
          readinessProbe:
            httpGet:
              path: /ready
              port: http
            initialDelaySeconds: 10
            periodSeconds: 10
          resources:
            requests:
              cpu: 100m
              memory: 256Mi
            limits:
              memory: 1Gi
          volumeMounts:
            - name: config
              mountPath: /etc/tempo
            - name: data
              mountPath: /var/tempo
      volumes:
        - name: config
          configMap:
            name: tempo
        - name: data
          persistentVolumeClaim:
            claimName: tempo
---
apiVersion: v1
kind: Service
metadata:
  name: tempo
  namespace: observability
  labels:
    app: tempo
spec:
  selector:
    app: tempo
  ports:
    - name: http
      port: 3200
      targetPort: http
    - name: otlp-grpc
      port: 4317
      targetPort: otlp-grpc
    - name: otlp-http
      port: 4318
      targetPort: otlp-http
"""


def _company_statements(companies: list[Company]) -> str:
    lines = []
    for company in companies:
        lines.append(
            f'- set(attributes["company"], "{company.id}") '
            f'where attributes["k8s.namespace.name"] == "{company.namespace}" and attributes["company"] == nil'
        )
    if not lines:
        lines.append('- set(attributes["company"], "unknown") where attributes["company"] == nil')
    return _indent("\n".join(lines), 10)


def collector_config(companies: list[Company]) -> str:
    """Unsamped spanmetrics, and a 10% trace sample forwarded to Tempo.

    Metric names match homelab.rules.SPAN_LATENCY and SPAN_CALLS. App metrics
    are not ingested: only the spanmetrics connector is exported to Prometheus.
    """
    statements = _company_statements(companies)
    return f"""\
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318
processors:
  memory_limiter:
    check_interval: 1s
    limit_mib: 512
    spike_limit_mib: 128
  k8sattributes:
    auth_type: serviceAccount
    passthrough: false
    extract:
      metadata:
        - k8s.namespace.name
        - k8s.pod.name
        - k8s.deployment.name
    pod_association:
      - sources:
          - from: resource_attribute
            name: k8s.pod.ip
      - sources:
          - from: connection
  transform:
    error_mode: ignore
    trace_statements:
      - context: resource
        statements:
{statements}
      - context: span
        statements:
          - set(attributes["company"], resource.attributes["company"]) where resource.attributes["company"] != nil
          - set(attributes["service"], resource.attributes["service.name"]) where resource.attributes["service.name"] != nil
  probabilistic_sampler:
    sampling_percentage: 10
connectors:
  spanmetrics:
    namespace: traces.spanmetrics
    dimensions:
      - name: company
      - name: service
    histogram:
      unit: ms
      explicit:
        buckets: [2ms, 5ms, 10ms, 25ms, 50ms, 100ms, 250ms, 500ms, 1s, 2s, 5s]
    exemplars:
      enabled: false
exporters:
  otlp/tempo:
    endpoint: tempo.observability.svc.cluster.local:4317
    tls:
      insecure: true
  prometheus:
    endpoint: 0.0.0.0:8889
service:
  pipelines:
    traces/spanmetrics:
      receivers: [otlp]
      processors: [memory_limiter, k8sattributes, transform]
      exporters: [spanmetrics]
    traces/tempo:
      receivers: [otlp]
      processors: [memory_limiter, k8sattributes, transform, probabilistic_sampler]
      exporters: [otlp/tempo]
    metrics/spanmetrics:
      receivers: [spanmetrics]
      exporters: [prometheus]
"""


def collector_manifest(companies: list[Company], image: str) -> str:
    config = collector_config(companies)
    return f"""\
apiVersion: v1
kind: ServiceAccount
metadata:
  name: otel-collector
  namespace: observability
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: homelab-otel-collector
rules:
  - apiGroups: [""]
    resources: ["pods", "namespaces", "nodes"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["apps"]
    resources: ["replicasets"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: homelab-otel-collector
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: homelab-otel-collector
subjects:
  - kind: ServiceAccount
    name: otel-collector
    namespace: observability
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: otel-collector
  namespace: observability
data:
  config.yaml: |
{_indent(config, 4)}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: otel-collector
  namespace: observability
  labels:
    app: otel-collector
spec:
  replicas: 1
  selector:
    matchLabels:
      app: otel-collector
  template:
    metadata:
      labels:
        app: otel-collector
    spec:
      serviceAccountName: otel-collector
      containers:
        - name: collector
          image: {image}
          args: ["--config=/conf/config.yaml"]
          ports:
            - name: otlp-grpc
              containerPort: 4317
            - name: otlp-http
              containerPort: 4318
            - name: prom
              containerPort: 8889
          resources:
            requests:
              cpu: 100m
              memory: 256Mi
            limits:
              memory: 1Gi
          volumeMounts:
            - name: config
              mountPath: /conf
      volumes:
        - name: config
          configMap:
            name: otel-collector
---
apiVersion: v1
kind: Service
metadata:
  name: otel-collector
  namespace: observability
  labels:
    app: otel-collector
spec:
  selector:
    app: otel-collector
  ports:
    - name: otlp-grpc
      port: 4317
      targetPort: otlp-grpc
    - name: otlp-http
      port: 4318
      targetPort: otlp-http
    - name: prom
      port: 8889
      targetPort: prom
---
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: otel-collector
  namespace: observability
  labels:
    app: otel-collector
spec:
  selector:
    matchLabels:
      app: otel-collector
  endpoints:
    - port: prom
      interval: 30s
"""


def alloy_manifest(image: str) -> str:
    config = """\
discovery.kubernetes "pods" {
  role = "pod"
}

discovery.relabel "company" {
  targets = discovery.kubernetes.pods.targets

  rule {
    source_labels = ["__meta_kubernetes_namespace"]
    regex         = "company-.+"
    action        = "keep"
  }

  rule {
    source_labels = ["__meta_kubernetes_namespace"]
    target_label  = "namespace"
  }

  rule {
    source_labels = ["__meta_kubernetes_pod_name"]
    target_label  = "pod"
  }

  rule {
    source_labels = ["__meta_kubernetes_pod_label_service"]
    target_label  = "service"
  }

  rule {
    source_labels = ["__meta_kubernetes_pod_label_app"]
    target_label  = "app"
  }

  rule {
    source_labels = ["__meta_kubernetes_pod_label_company"]
    target_label  = "company"
  }
}

loki.source.kubernetes "pods" {
  targets    = discovery.relabel.company.output
  forward_to = [loki.write.local.receiver]
}

loki.write "local" {
  endpoint {
    url = "http://loki.observability.svc.cluster.local:3100/loki/api/v1/push"
  }
}
"""
    return f"""\
apiVersion: v1
kind: ServiceAccount
metadata:
  name: alloy
  namespace: observability
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: homelab-alloy
rules:
  - apiGroups: [""]
    resources: ["pods", "pods/log", "namespaces", "nodes"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: homelab-alloy
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: homelab-alloy
subjects:
  - kind: ServiceAccount
    name: alloy
    namespace: observability
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: alloy
  namespace: observability
data:
  config.alloy: |
{_indent(config, 4)}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: alloy
  namespace: observability
  labels:
    app: alloy
spec:
  replicas: 1
  selector:
    matchLabels:
      app: alloy
  template:
    metadata:
      labels:
        app: alloy
    spec:
      serviceAccountName: alloy
      containers:
        - name: alloy
          image: {image}
          args: ["run", "/etc/alloy/config.alloy", "--server.http.listen-addr=0.0.0.0:12345"]
          ports:
            - name: http
              containerPort: 12345
          resources:
            requests:
              cpu: 50m
              memory: 128Mi
            limits:
              memory: 512Mi
          volumeMounts:
            - name: config
              mountPath: /etc/alloy
      volumes:
        - name: config
          configMap:
            name: alloy
"""


def render(companies: list[Company], dest: Path, versions: dict[str, str] | None = None) -> None:
    pins = versions or load_versions()
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "namespaces.yaml").write_text(NAMESPACES, encoding="utf-8")
    (dest / "loki.yaml").write_text(loki_manifest(pins["LOKI_IMAGE"]), encoding="utf-8")
    (dest / "tempo.yaml").write_text(tempo_manifest(pins["TEMPO_IMAGE"]), encoding="utf-8")
    (dest / "otel-collector.yaml").write_text(collector_manifest(companies, pins["COLLECTOR_IMAGE"]), encoding="utf-8")
    (dest / "alloy.yaml").write_text(alloy_manifest(pins["ALLOY_IMAGE"]), encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Render Loki, Tempo, Alloy and the collector. No cluster.")
    parser.add_argument("--companies-file", type=Path, default=None)
    parser.add_argument("--companies", default="")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    selected = select_companies(load_companies(args.companies_file), args.companies or None)
    render(selected, args.out)
    print(f"rendered observability for {len(selected)} companies into {args.out}")


if __name__ == "__main__":
    main()
