from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2] / "deploy" / "kubernetes"


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
        assert pod["securityContext"]["runAsNonRoot"] is True
        assert pod["containers"][0]["securityContext"]["readOnlyRootFilesystem"] is True
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
    assert "OPENMONTAGE_OSS_BUCKET, value: openmontage-oss" in kustomization
    assert "OPENMONTAGE_OSS_REGION, value: us-east-1" in kustomization
    assert "OPENMONTAGE_OSS_ENDPOINT, value: oss-us-east-1.aliyuncs.com" in kustomization
    assert "ALIBABA_CLOUD_ROLE_ARN" not in kustomization
    assert "ALIBABA_CLOUD_OIDC_PROVIDER_ARN" not in kustomization
    assert "ALIBABA_CLOUD_OIDC_TOKEN_FILE" not in kustomization


def test_legacy_test_manifest_targets_openmontage_namespace():
    for doc in docs(ROOT / "openmontage-test.yaml"):
        if doc.get("kind") == "Namespace":
            continue
        assert doc["metadata"].get("namespace") == "openmontage-test"
