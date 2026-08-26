from __future__ import annotations

from pathlib import Path

from tools.base_tool import ToolStatus
from tools.pipeline_catalog import _entry_for_manifest, build_pipeline_catalog, pipeline_catalog_payload, require_ready_pipeline


class _Tool:
    def __init__(self, status: ToolStatus):
        self.status = status

    def get_status(self):
        return self.status

    def get_info(self):
        return {"dependencies": []}


class _Registry:
    def __init__(self, tools):
        self.tools = tools

    def get(self, name):
        return self.tools.get(name)


def test_catalog_lists_every_manifest_and_safe_fields():
    entries = build_pipeline_catalog()
    names = {entry.name for entry in entries}
    assert {"cinematic", "animated-explainer", "framework-smoke"}.issubset(names)
    assert all(entry.status in {"READY", "DEGRADED", "DISABLED"} for entry in entries)
    assert all("api_key" not in entry.as_dict() for entry in entries)
    assert all("prompt" not in entry.as_dict() for entry in entries)


def test_screen_demo_manifest_is_valid_and_not_reported_as_manifest_error():
    screen_demo = next(item for item in build_pipeline_catalog() if item.name == "screen-demo")
    assert screen_demo.reason_code != "manifest_invalid"


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


def test_optional_tools_do_not_disable_a_pipeline():
    manifest = {
        "name": "optional-ready",
        "stages": [{"name": "compose", "required_tools": ["compose"], "optional_tools": ["premium"]}],
    }
    entry = _entry_for_manifest(manifest, _Registry({"compose": _Tool(ToolStatus.AVAILABLE)}))
    assert entry.status == "READY"


def test_missing_required_tool_disables_a_pipeline():
    manifest = {"name": "required-missing", "stages": [{"name": "compose", "required_tools": ["compose"]}]}
    entry = _entry_for_manifest(manifest, _Registry({}))
    assert entry.status == "DISABLED"
    assert entry.reason_code == "required_capability_unavailable"
