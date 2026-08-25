from __future__ import annotations

from pathlib import Path

from tools.pipeline_catalog import build_pipeline_catalog, pipeline_catalog_payload, require_ready_pipeline


def test_catalog_lists_every_manifest_and_safe_fields():
    entries = build_pipeline_catalog()
    names = {entry.name for entry in entries}
    assert {"cinematic", "animated-explainer", "framework-smoke"}.issubset(names)
    assert all(entry.status in {"READY", "DEGRADED", "DISABLED"} for entry in entries)
    assert all("api_key" not in entry.as_dict() for entry in entries)
    assert all("prompt" not in entry.as_dict() for entry in entries)


def test_catalog_payload_is_versioned_and_sorted():
    payload = pipeline_catalog_payload()
    assert payload["schemaVersion"] == "openmontage.catalog.v1"
    names = [entry["name"] for entry in payload["pipelines"]]
    assert names == sorted(names)


def test_require_ready_pipeline_fails_closed_for_unknown_pipeline(tmp_path: Path):
    try:
        require_ready_pipeline("does-not-exist", defs_dir=tmp_path)
    except ValueError as exc:
        assert str(exc) == "openmontage_pipeline_not_found"
    else:
        raise AssertionError("unknown pipeline should be rejected")


def test_test_manifests_are_not_user_selectable():
    smoke = next(item for item in build_pipeline_catalog() if item.name == "framework-smoke")
    assert smoke.status == "DISABLED"
    assert smoke.reason_code == "test_pipeline_not_user_selectable"
