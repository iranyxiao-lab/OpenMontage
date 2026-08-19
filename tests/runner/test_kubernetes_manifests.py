import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2] / "deploy" / "kubernetes"
MANIFEST_ROOT = Path(__file__).resolve().parents[2] / "openmontage" / "runner" / "manifests"


def docs(path: Path):
    return [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc]


def test_base_has_isolated_pools_and_no_public_service():
    all_docs = docs(ROOT / "base" / "workers.yaml") + docs(ROOT / "base" / "metrics-service.yaml")
    deployments = [doc for doc in all_docs if doc.get("kind") == "Deployment"]
    names = {doc["metadata"]["name"] for doc in deployments}
    assert {"openmontage-planner-stable", "openmontage-planner-canary", "openmontage-render-stable", "openmontage-render-canary", "openmontage-gpu-stable"} <= names
    assert next(doc for doc in deployments if doc["metadata"]["name"] == "openmontage-gpu-stable")["spec"]["replicas"] == 0
    for deployment in deployments:
        pod = deployment["spec"]["template"]["spec"]
        container = pod["containers"][0]
        assert pod["securityContext"]["runAsNonRoot"] is True
        assert container["securityContext"]["readOnlyRootFilesystem"] is True
        assert {mount["mountPath"] for mount in container["volumeMounts"]} >= {"/tmp"}
        tmp_volume = next(volume for volume in pod["volumes"] if volume["name"] == "tmp")
        assert tmp_volume["emptyDir"]["sizeLimit"]
        environment = {item["name"]: item.get("value") for item in container.get("env", [])}
        if deployment["metadata"]["name"] != "openmontage-gpu-stable":
            assert environment["OPENMONTAGE_MANIFEST_ROOT"] == "/workspace/openmontage/runner/manifests"
    service = next(doc for doc in all_docs if doc["kind"] == "Service")
    assert service["spec"]["type"] == "ClusterIP"


def test_environment_overlays_are_explicit_namespaces_and_restricted():
    for environment in ("test", "prod"):
        overlay = ROOT / "overlays" / environment
        namespace = next(doc for doc in docs(overlay / "namespace.yaml") if doc["kind"] == "Namespace")
        assert namespace["metadata"]["name"] == f"openmontage-{environment}"
        assert namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"
    for path in (ROOT / "base").glob("*.yaml"):
        for doc in docs(path):
            if doc.get("kind") not in {"Namespace", "Deployment", "ServiceAccount", "Service", "NetworkPolicy", "ResourceQuota", "LimitRange", "PodDisruptionBudget"}:
                continue
            if doc["kind"] != "Namespace":
                assert doc["metadata"].get("namespace") == "openmontage-test"


def test_network_policy_is_default_deny():
    policies = docs(ROOT / "base" / "policies.yaml")
    deny = next(doc for doc in policies if doc["metadata"]["name"] == "openmontage-default-deny")
    assert set(deny["spec"]["policyTypes"]) == {"Ingress", "Egress"}


def test_oss_uses_virginia_environment_credentials_without_oidc_role_config():
    kustomization = (ROOT / "base" / "kustomization.yaml").read_text(encoding="utf-8")
    assert "name: deepthink-docker-registry-key" in kustomization
    assert "OPENMONTAGE_OSS_BUCKET, value: openmontage-oss" in kustomization
    assert "OPENMONTAGE_OSS_REGION, value: us-east-1" in kustomization
    assert "OPENMONTAGE_OSS_ENDPOINT, value: oss-us-east-1.aliyuncs.com" in kustomization
    assert "name: OPENMONTAGE_REDIS_URL" in kustomization
    assert "name: openmontage-redis" in kustomization
    assert "key: url" in kustomization
    assert "ALIBABA_CLOUD_ROLE_ARN" not in kustomization
    assert "ALIBABA_CLOUD_OIDC_PROVIDER_ARN" not in kustomization
    assert "ALIBABA_CLOUD_OIDC_TOKEN_FILE" not in kustomization


def test_legacy_test_manifest_targets_openmontage_namespace():
    for doc in docs(ROOT / "openmontage-test.yaml"):
        if doc.get("kind") == "Namespace":
            continue
        assert doc["metadata"].get("namespace") == "openmontage-test"


def test_test_overlay_uses_minimal_worker_resources():
    overlay_path = ROOT / "overlays" / "test" / "kustomization.yaml"
    overlay = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
    replica_patch = next(
        patch for patch in overlay["patches"]
        if patch["target"].get("name") == "openmontage-(planner|render)-stable"
    )
    assert yaml.safe_load(replica_patch["patch"]) == [
        {"op": "replace", "path": "/spec/replicas", "value": 1}
    ]
    planner_patch = next(
        patch for patch in overlay["patches"]
        if patch["target"].get("name") == "openmontage-planner-(stable|canary)"
    )
    assert yaml.safe_load(planner_patch["patch"]) == [
        {
            "op": "replace",
            "path": "/spec/template/spec/containers/0/resources",
            "value": {
                "requests": {
                    "cpu": "100m",
                    "memory": "128Mi",
                    "ephemeral-storage": "256Mi",
                },
                "limits": {
                    "cpu": "500m",
                    "memory": "256Mi",
                    "ephemeral-storage": "1Gi",
                },
            },
        },
        {
            "op": "replace",
            "path": "/spec/template/spec/volumes/0/emptyDir/sizeLimit",
            "value": "1Gi",
        },
    ]
    render_patch = next(
        patch for patch in overlay["patches"]
        if patch["target"].get("name") == "openmontage-render-.*"
    )
    operations = yaml.safe_load(render_patch["patch"])
    assert operations == [
        {
            "op": "replace",
            "path": "/spec/template/spec/containers/0/resources",
            "value": {
                "requests": {
                    "cpu": "250m",
                    "memory": "512Mi",
                    "ephemeral-storage": "1Gi",
                },
                "limits": {
                    "cpu": "2",
                    "memory": "2Gi",
                    "ephemeral-storage": "8Gi",
                },
            },
        },
        {
            "op": "replace",
            "path": "/spec/template/spec/volumes/0/emptyDir/sizeLimit",
            "value": "8Gi",
        },
    ]


def test_cluster_manifest_is_versioned_and_declares_all_stages():
    versions = [path for path in MANIFEST_ROOT.iterdir() if path.is_dir()]
    assert len(versions) == 1
    manifest = json.loads((versions[0] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifestVersion"] == f"sha256:{versions[0].name}"
    assert set(manifest["stages"]) == {
        "intake", "research", "proposal", "script", "scene_plan",
        "assets", "edit", "compose", "publish",
    }


@pytest.mark.parametrize("environment", ["test", "prod"])
def test_environment_overlay_renders(environment):
    kubectl = shutil.which("kubectl")
    if kubectl is None:
        pytest.skip("kubectl is required to render Kustomize overlays")
    result = subprocess.run(
        [kubectl, "kustomize", str(ROOT / "overlays" / environment)],
        check=True,
        capture_output=True,
        text=True,
    )
    rendered = list(yaml.safe_load_all(result.stdout))
    assert rendered
    assert all(
        doc["metadata"].get("namespace") == f"openmontage-{environment}"
        for doc in rendered
        if doc and doc.get("kind") != "Namespace"
    )
