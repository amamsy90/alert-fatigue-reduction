"""The Helm chart keeps the safety rules of the product: shadow by default, a password, one replica.

Skipped when the helm binary is not installed (CI installs it).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).parents[1] / "deploy" / "helm" / "oncall-queue"
HELM = shutil.which("helm")
pytestmark = pytest.mark.skipif(HELM is None, reason="helm is not installed")


PASSWORD = ("--set-string", "auth.password=x")


def render(*args: str, expect_failure: bool = False):
    cmd = [HELM, "template", "rel", str(CHART), "--namespace", "ns", *args]
    done = subprocess.run(cmd, capture_output=True, text=True)
    if expect_failure:
        assert done.returncode != 0
        return done.stderr
    assert done.returncode == 0, done.stderr
    return [d for d in yaml.safe_load_all(done.stdout) if d]


def by_kind(docs, kind):
    return [d for d in docs if d["kind"] == kind]


def test_chart_version_matches_the_app():
    from product import __version__

    chart = yaml.safe_load((CHART / "Chart.yaml").read_text(encoding="utf-8"))
    assert chart["appVersion"] == __version__


def test_a_password_is_required():
    assert "auth.password is required" in render(expect_failure=True)


def test_an_existing_secret_replaces_the_password_and_creates_no_secret():
    docs = render("--set", "auth.existingSecret=mine")
    assert by_kind(docs, "Secret") == []
    env = {e["name"]: e for e in by_kind(docs, "Deployment")[0]["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["APP_PASSWORD"]["valueFrom"]["secretKeyRef"]["name"] == "mine"


def test_shadow_mode_is_the_default_and_the_switch_works():
    def shadow(docs):
        env = by_kind(docs, "Deployment")[0]["spec"]["template"]["spec"]["containers"][0]["env"]
        return next(e["value"] for e in env if e["name"] == "SHADOW_MODE")

    assert shadow(render(*PASSWORD)) == "1"
    assert shadow(render(*PASSWORD, "--set", "shadowMode=false")) == "0"


def test_one_replica_recreated_never_rolled():
    spec = by_kind(render(*PASSWORD), "Deployment")[0]["spec"]
    assert spec["replicas"] == 1 and spec["strategy"]["type"] == "Recreate"


def test_the_pod_is_locked_down():
    pod = by_kind(render(*PASSWORD), "Deployment")[0]["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] is True and pod["securityContext"]["runAsUser"] == 10001
    ctr = pod["containers"][0]
    assert ctr["securityContext"]["allowPrivilegeEscalation"] is False
    assert ctr["securityContext"]["readOnlyRootFilesystem"] is True
    assert ctr["securityContext"]["capabilities"]["drop"] == ["ALL"]
    mounts = {m["mountPath"] for m in ctr["volumeMounts"]}
    # Every place the app writes must be a volume, because the root filesystem is read-only.
    assert {"/app/product/data", "/tmp", "/app/data/sample_product"} <= mounts
    for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
        assert ctr[probe]["httpGet"]["path"] == "/health"


def test_the_password_lives_only_in_the_secret():
    docs = render("--set-string", "auth.password=hunter2-unique")
    text = yaml.safe_dump(by_kind(docs, "Deployment") + by_kind(docs, "Service") + by_kind(docs, "PersistentVolumeClaim"))
    assert "hunter2-unique" not in text
    assert by_kind(docs, "Secret")[0]["stringData"]["APP_PASSWORD"] == "hunter2-unique"


def test_the_default_image_is_the_release_tag():
    from product import __version__

    ctr = by_kind(render(*PASSWORD), "Deployment")[0]["spec"]["template"]["spec"]["containers"][0]
    assert ctr["image"].endswith(f":v{__version__}")


def test_data_survives_a_release_rollback():
    pvc = by_kind(render(*PASSWORD), "PersistentVolumeClaim")[0]
    assert pvc["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"


def test_service_names_are_accepted_as_hosts():
    env = by_kind(render(*PASSWORD), "Deployment")[0]["spec"]["template"]["spec"]["containers"][0]["env"]
    hosts = next(e["value"] for e in env if e["name"] == "ALLOWED_HOSTS").split(",")
    assert {"rel-oncall-queue", "rel-oncall-queue.ns.svc", "localhost"} <= set(hosts)


def test_retrain_is_off_by_default_and_a_forbid_cronjob_when_on():
    assert by_kind(render(*PASSWORD), "CronJob") == []
    job = by_kind(render(*PASSWORD, "--set", "retrain.enabled=true"), "CronJob")[0]["spec"]
    assert job["concurrencyPolicy"] == "Forbid" and job["schedule"] == "17 */6 * * *"
    command = " ".join(job["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["command"])
    assert "/models/retrain" in command and "rel-oncall-queue:8080" in command


def test_monitoring_objects_are_optional():
    docs = render(*PASSWORD)
    assert by_kind(docs, "ServiceMonitor") == [] and by_kind(docs, "PrometheusRule") == []
    docs = render(*PASSWORD, "--set", "prometheusRule.enabled=true")
    alerts = [r["alert"] for r in by_kind(docs, "PrometheusRule")[0]["spec"]["groups"][0]["rules"]]
    assert {"OncallQueueInputDrift", "OncallModelStale", "OncallRetrainRolledBack"} <= set(alerts)
