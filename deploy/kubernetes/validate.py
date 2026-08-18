"""Static manifest gate; does not contact a Kubernetes cluster."""
from pathlib import Path
import sys

import yaml


NAMESPACES = {"test": "openmontage-test", "prod": "openmontage-prod"}
NAMESPACED_KINDS = {"Deployment", "Service", "ServiceAccount", "NetworkPolicy", "ResourceQuota", "LimitRange", "PodDisruptionBudget", "HorizontalPodAutoscaler"}


def load(path: Path):
    return [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc]


def validate(environment: str) -> None:
    root = Path(__file__).parent
    expected = NAMESPACES[environment]
    overlay = root / "overlays" / environment
    namespace = next(doc for doc in load(overlay / "namespace.yaml") if doc.get("kind") == "Namespace")
    assert namespace["metadata"]["name"] == expected
    for path in (root / "base").glob("*.yaml"):
        for doc in load(path):
            if doc.get("kind") in NAMESPACED_KINDS:
                assert doc["metadata"].get("namespace") == "openmontage-test", f"missing base namespace: {path}"
                assert doc.get("spec", {}).get("type") != "LoadBalancer"
            if doc.get("kind") == "PersistentVolumeClaim":
                assert "ReadWriteMany" not in doc.get("spec", {}).get("accessModes", [])


if __name__ == "__main__":
    for env in sys.argv[1:] or NAMESPACES:
        validate(env)
        print(f"{env}: static manifest checks passed")
