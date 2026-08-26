"""Runtime pipeline and capability catalog for the management console."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from functools import lru_cache

from lib.pipeline_loader import (
    PIPELINE_DEFS_DIR,
    get_stage_order,
    list_pipelines,
    load_pipeline,
    pipeline_supports_reference_input,
)
from tools.base_tool import ToolStatus
from tools.tool_registry import ToolRegistry, registry as default_registry

CATALOG_SCHEMA = "openmontage.catalog.v1"


@dataclass(frozen=True)
class PipelineCatalogEntry:
    name: str
    version: str
    description: str
    category: str
    stability: str
    status: str
    reason_code: str | None
    stages: tuple[str, ...]
    input_types: tuple[str, ...]
    dependencies: tuple[str, ...]
    estimated_duration_minutes: int | None
    budget_default_usd: float | None
    max_wall_time_minutes: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "category": self.category,
            "stability": self.stability,
            "status": self.status,
            "reasonCode": self.reason_code,
            "stages": list(self.stages),
            "inputTypes": list(self.input_types),
            "dependencies": list(self.dependencies),
            "estimatedDurationMinutes": self.estimated_duration_minutes,
            "budgetDefaultUsd": self.budget_default_usd,
            "maxWallTimeMinutes": self.max_wall_time_minutes,
        }


def _tool_status(tool: Any) -> str:
    try:
        status = tool.get_status()
    except Exception:
        return ToolStatus.UNAVAILABLE.value
    return status.value if isinstance(status, ToolStatus) else str(status)


def _candidate_tools(manifest: dict[str, Any]) -> Iterable[tuple[str, list[str]]]:
    for mode in manifest.get("production_modes", []) or []:
        names: list[str] = []
        for key in ("required_tools", "optional_tools", "tools_available"):
            for name in mode.get(key, []) or []:
                if isinstance(name, str) and name and name not in names:
                    names.append(name)
        if names:
            yield f"mode:{mode.get('name', 'unknown')}", names
    for stage in manifest.get("stages", []):
        names: list[str] = []
        for key in ("tools_available", "preferred_tools", "fallback_tools", "optional_tools"):
            for name in stage.get(key, []) or []:
                if isinstance(name, str) and name and name not in names:
                    names.append(name)
        if names:
            yield str(stage.get("name", "unknown")), names
    reference = manifest.get("reference_input") or {}
    names = [name for name in reference.get("analysis_tools", []) or [] if isinstance(name, str)]
    if names:
        yield "reference_input", names


def _required_tool_groups(manifest: dict[str, Any]) -> Iterable[tuple[str, list[str]]]:
    for stage in manifest.get("stages", []):
        names = [name for name in stage.get("required_tools", []) or [] if isinstance(name, str) and name]
        if names:
            yield str(stage.get("name", "unknown")), names


def _entry_for_manifest(manifest: dict[str, Any], tool_registry: ToolRegistry) -> PipelineCatalogEntry:
    missing: list[str] = []
    degraded: list[str] = []
    dependencies: set[str] = set()
    for stage_name, candidates in _required_tool_groups(manifest):
        statuses: list[str] = []
        for name in candidates:
            tool = tool_registry.get(name)
            if tool is None:
                statuses.append(ToolStatus.UNAVAILABLE.value)
                continue
            statuses.append(_tool_status(tool))
            try:
                dependencies.update(str(value) for value in tool.get_info().get("dependencies", []) or [] if isinstance(value, str))
            except Exception:
                pass
        if all(status == ToolStatus.AVAILABLE.value for status in statuses):
            continue
        if statuses and all(status in {ToolStatus.AVAILABLE.value, ToolStatus.DEGRADED.value} for status in statuses):
            degraded.append(stage_name)
        else:
            missing.append(stage_name)
    for _, candidates in _candidate_tools(manifest):
        for name in candidates:
            tool = tool_registry.get(name)
            if tool is None:
                continue
            try:
                dependencies.update(str(value) for value in tool.get_info().get("dependencies", []) or [] if isinstance(value, str))
            except Exception:
                pass

    if str(manifest.get("name", "")) == "framework-smoke" or str(manifest.get("stability", "")).lower() == "test":
        status, reason = "DISABLED", "test_pipeline_not_user_selectable"
    elif missing:
        status, reason = "DISABLED", "required_capability_unavailable"
    elif degraded:
        status, reason = "DEGRADED", "fallback_capability_selected"
    else:
        status, reason = "READY", None

    orchestration = manifest.get("orchestration") or {}
    duration = orchestration.get("max_wall_time_minutes")
    default_budget = orchestration.get("budget_default_usd")
    input_types = list(manifest.get("input_types") or ["prompt"])
    if pipeline_supports_reference_input(manifest) and "reference_video" not in input_types:
        input_types.append("reference_video")
    if "asset" not in input_types:
        input_types.append("asset")
    return PipelineCatalogEntry(
        name=str(manifest.get("name", "")),
        version=str(manifest.get("version", "1.0")),
        description=" ".join(str(manifest.get("description", "")).split()),
        category=str(manifest.get("category", "custom")),
        stability=str(manifest.get("stability", "beta")),
        status=status,
        reason_code=reason,
        stages=tuple(get_stage_order(manifest)),
        input_types=tuple(input_types),
        dependencies=tuple(sorted(dependencies)),
        estimated_duration_minutes=int(duration) if isinstance(duration, (int, float)) else None,
        budget_default_usd=float(default_budget) if isinstance(default_budget, (int, float)) else None,
        max_wall_time_minutes=int(duration) if isinstance(duration, (int, float)) else None,
    )


def build_pipeline_catalog(defs_dir: Path | None = None, tool_registry: ToolRegistry | None = None) -> list[PipelineCatalogEntry]:
    """Build every manifest entry with a live readiness state."""
    defs_dir = defs_dir or PIPELINE_DEFS_DIR
    tool_registry = tool_registry or default_registry
    tool_registry.ensure_discovered()
    entries: list[PipelineCatalogEntry] = []
    for name in sorted(list_pipelines(defs_dir)):
        try:
            entries.append(_entry_for_manifest(load_pipeline(name, defs_dir), tool_registry))
        except Exception:
            entries.append(PipelineCatalogEntry(
                name=name, version="unknown", description="", category="unknown", stability="unknown",
                status="DISABLED", reason_code="manifest_invalid", stages=(), input_types=("prompt",),
                dependencies=(), estimated_duration_minutes=None, budget_default_usd=None, max_wall_time_minutes=None,
            ))
    return entries


def pipeline_catalog_payload(defs_dir: Path | None = None, tool_registry: ToolRegistry | None = None) -> dict[str, Any]:
    entries = build_pipeline_catalog(defs_dir=defs_dir, tool_registry=tool_registry)
    models: list[dict[str, Any]] = []
    try:
        from tools.gateway_client import GatewayClient, gateway_configured
        if gateway_configured():
            for item in GatewayClient().catalog().values():
                models.append({
                    "modelId": item.model_id,
                    "provider": item.channel,
                    "channel": item.channel,
                    "capability": item.capability,
                    "status": item.status,
                    "reasonCode": item.reason_code,
                    "inputTypes": list(item.input_types),
                    "requiredInputTypes": list(item.required_input_types),
                })
    except Exception:
        # Model probing is advisory; pipeline readiness remains independently
        # visible, while explicit model selection fails closed downstream.
        models = []
    models.sort(key=lambda item: item["modelId"])
    return {
        "schemaVersion": CATALOG_SCHEMA,
        "pipelines": [entry.as_dict() for entry in entries],
        "models": models,
    }


@lru_cache(maxsize=1)
def cached_pipeline_catalog_payload() -> dict[str, Any]:
    """Build the live catalog once per worker process."""
    return pipeline_catalog_payload()


def require_ready_pipeline(name: str, defs_dir: Path | None = None, tool_registry: ToolRegistry | None = None) -> PipelineCatalogEntry:
    for entry in build_pipeline_catalog(defs_dir=defs_dir, tool_registry=tool_registry):
        if entry.name == name:
            if entry.status != "READY":
                raise ValueError(f"openmontage_pipeline_disabled:{entry.reason_code}")
            return entry
    raise ValueError("openmontage_pipeline_not_found")
