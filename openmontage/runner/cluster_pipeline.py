from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from tools.video.sora_video import SoraVideo
from tools.gateway_client import GatewayClient, GatewayRequestError, gateway_configured
from tools.gateway_model_catalog import GatewayModel
from tools.tool_registry import registry
from lib.pipeline_loader import load_pipeline


_STAGES = {
    "intake",
    "idea",
    "research",
    "proposal",
    "script",
    "scene_plan",
    "character_design",
    "rig_plan",
    "assets",
    "edit",
    "compose",
    "publish",
}

_STAGE_ARTIFACT_NAMES = {
    "intake": "intake-brief.json",
    "idea": "idea-brief.json",
    "research": "research-brief.json",
    "proposal": "proposal-packet.json",
    "script": "script.json",
    "scene_plan": "scene-plan.json",
    "character_design": "character-design.json",
    "rig_plan": "rig-plan.json",
    "assets": "asset-manifest.json",
    "edit": "edit-decisions.json",
    "compose": "render-report.json",
    "publish": "publish-log.json",
}


def run(command: Any, workspace: Path) -> bytes:
    """Execute the pinned cluster pipeline without exposing provider payloads."""

    if command.stage not in _STAGES:
        raise RuntimeError("manifest_stage_unsupported")

    checkpoint: dict[str, Any] = {
        "schemaVersion": "openmontage.checkpoint.v1",
        "stage": command.stage,
        "pipelineType": command.pipelineType,
        "runRevision": command.runRevision,
    }
    if command.stage != "publish":
        intent = command.userIntent
        if intent is None:
            raise RuntimeError("stage_user_intent_missing")
        artifact = _stage_artifact(command, intent, workspace)
        artifact_name = _STAGE_ARTIFACT_NAMES[command.stage]
        artifact_dir = workspace / "artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_bytes = _encode(artifact)
        (artifact_dir / artifact_name).write_bytes(artifact_bytes)
        checkpoint["result"] = {
            "artifactType": artifact["artifactType"],
            "artifactStatus": "completed",
            "artifactName": artifact_name,
            "sceneCount": len(artifact.get("scenes", [])),
            "mode": "multi_scene" if len(artifact.get("scenes", [])) > 1 else "single_scene",
        }
        return _encode(checkpoint)

    intent = command.userIntent
    if intent is None:
        raise RuntimeError("publish_user_intent_missing")

    output = workspace / "renders" / "final.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    model = (intent.production.model or os.getenv("OPENMONTAGE_VIDEO_MODEL", "sora-2")).strip() or "sora-2"
    reference_path = _reference_image(workspace)
    if model in {"sora-2", "sora-2-pro"}:
        _enforce_budget_policy(intent, model)
        scene_plan = _build_scene_plan(intent.production.durationSeconds, 12)
        _generate_sora_scenes(model, intent, scene_plan, output, reference_path)
        provider = "openai"
        route = "/v1/videos"
    else:
        if not gateway_configured():
            raise RuntimeError("gateway_configuration_missing")
        scene_plan = _build_scene_plan(intent.production.durationSeconds, 10)
        try:
            provider, route = _generate_gateway_scenes(model, intent, scene_plan, output, workspace)
        except GatewayRequestError as exc:
            raise RuntimeError("gateway_video_generation_failed") from exc

    actual = _apply_production_contract(output, intent, workspace)
    checkpoint["result"] = {
        "name": output.name,
        "contentType": "video/mp4",
        "provider": provider,
        "model": model,
        "route": route,
        "scenePlan": scene_plan,
        "production": actual,
    }
    artifact_dir = workspace / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / _STAGE_ARTIFACT_NAMES["publish"]).write_bytes(_encode({
        "schemaVersion": "openmontage.artifact.v1",
        "artifactType": "publish_log",
        "stage": "publish",
        "runRevision": command.runRevision,
        "sceneCount": len(scene_plan),
        "scenes": scene_plan,
        "deliverables": [{"name": output.name, "contentType": "video/mp4"}],
        "production": actual,
    }))
    return _encode(checkpoint)


def _stage_artifact(command: Any, intent: Any, workspace: Path) -> dict[str, Any]:
    """Build a bounded canonical artifact for a single manifest stage."""

    manifest_stage = _manifest_stage(command.pipelineType, command.stage)
    plan = _build_scene_plan(intent.production.durationSeconds, 12)
    narration = (intent.narrationText or "").strip()
    source_inventory = _source_inventory(intent, workspace)
    base: dict[str, Any] = {
        "schemaVersion": "openmontage.artifact.v1",
        "artifactType": {
            "intake": "brief",
            "idea": "brief",
            "research": "research_brief",
            "proposal": "proposal_packet",
            "script": "script",
            "scene_plan": "scene_plan",
            "character_design": "character_design",
            "rig_plan": "rig_plan",
            "assets": "asset_manifest",
            "edit": "edit_decisions",
            "compose": "render_report",
        }.get(command.stage, next(iter(manifest_stage.get("produces", [])), "stage_output")),
        "stage": command.stage,
        "pipelineType": command.pipelineType,
        "runRevision": command.runRevision,
        "production": intent.production.model_dump(mode="json"),
        "regeneration": _regeneration_summary(intent),
        "director": manifest_stage,
    }
    if command.stage in {"intake", "idea"}:
        base.update({
            "brief": intent.brief,
            "narration": {"provided": bool(narration), "text": narration or None},
            "sources": source_inventory,
        })
    elif command.stage == "research":
        base.update({
            "sources": source_inventory,
            "findings": _research_findings(source_inventory),
            "constraints": _production_constraints(intent),
        })
    elif command.stage == "proposal":
        composition = _composition_options()
        base.update({
            "summary": intent.brief,
            "estimatedCostUsd": _estimated_cost(intent),
            "approvalRequired": True,
            "composition": {
                "recommendedRuntime": composition[0],
                "options": composition,
                "selectionPolicy": "approval_accepts_recommendation",
            },
            "stages": ["research", "proposal", "script", "scene_plan", "assets", "edit", "compose", "publish"],
        })
    elif command.stage == "script":
        base.update({
            "narrationProvided": bool(narration),
            "narrationText": narration or None,
            "visualBrief": intent.brief,
            "beats": _script_beats(plan, narration),
        })
    elif command.stage == "scene_plan":
        base.update({"approvalRequired": True, "scenes": _scene_artifacts(plan, intent, narration)})
    elif command.stage == "assets":
        base.update({
            "sources": source_inventory,
            "scenes": [{
                "sceneId": scene["sceneId"],
                "status": "planned",
                "bindings": [item["sourceId"] for item in source_inventory],
                "generation": {"model": intent.production.model, "strategy": "automatic" if not intent.production.model else "fixed"},
            } for scene in _scene_artifacts(plan, intent, narration) if _scene_selected(intent, scene["sceneId"])],
        })
    elif command.stage == "edit":
        base.update({
            "scenes": [{
                "sceneId": scene["sceneId"],
                "startSeconds": scene["startSeconds"],
                "endSeconds": scene["endSeconds"],
                "transition": "cut" if scene["index"] == 1 else "crossfade",
            } for scene in _scene_artifacts(plan, intent, narration)],
            "audio": {"narration": bool(narration), "music": intent.production.music, "subtitles": intent.production.subtitleStyle},
        })
    elif command.stage == "compose":
        base.update({
            "scenes": _scene_artifacts(plan, intent, narration),
            "render": {"status": "ready", "contentType": "video/mp4", "resolution": intent.production.resolution},
        })
    directed = _directed_stage_output(command, intent, workspace, manifest_stage)
    if directed is not None:
        base["directorOutput"] = directed["output"]
        base["directorModel"] = directed["model"]
        base["directorProvider"] = directed["provider"]
    return base


def _manifest_stage(pipeline_type: str, stage: str) -> dict[str, Any]:
    manifest = load_pipeline(pipeline_type)
    if stage == "intake":
        return {"skill": "meta/onboarding", "reviewFocus": [], "successCriteria": ["validated user intent"]}
    definition = next((item for item in manifest.get("stages", []) if item.get("name") == stage), None)
    if definition is None:
        raise RuntimeError("manifest_stage_unsupported")
    return {
        "skill": definition.get("skill"),
        "produces": list(definition.get("produces", []) or []),
        "reviewFocus": list(definition.get("review_focus", []) or []),
        "successCriteria": list(definition.get("success_criteria", []) or []),
        "approvalRequired": bool(definition.get("human_approval_default", False)),
    }


def _directed_stage_output(command: Any, intent: Any, workspace: Path,
                           manifest_stage: dict[str, Any]) -> dict[str, Any] | None:
    """Execute the manifest's director skill through a gateway chat model."""

    if command.stage == "intake" or not gateway_configured():
        return None
    skill_name = manifest_stage.get("skill")
    if not isinstance(skill_name, str) or not skill_name:
        raise RuntimeError("manifest_stage_skill_missing")
    root = Path(__file__).resolve().parents[2]
    skill_path = root / "skills" / (skill_name + ".md")
    if not skill_path.is_file():
        raise RuntimeError("manifest_stage_skill_missing")
    skill_text = skill_path.read_text(encoding="utf-8")[:24_000]
    prior = _prior_artifact_context(workspace)
    client = GatewayClient()
    catalog = client.catalog()
    chat_models = [item for item in catalog.values() if item.capability == "chat"]
    if not chat_models:
        raise RuntimeError("stage_director_model_unavailable")
    selected = next((item for item in chat_models if item.model_id == "qwen-flash"), chat_models[0])
    request = {
        "pipeline": command.pipelineType,
        "stage": command.stage,
        "brief": intent.brief,
        "narrationText": intent.narrationText,
        "production": intent.production.model_dump(mode="json"),
        "sources": [{"kind": source.kind, "hasObject": bool(source.objectKey), "hasLink": bool(source.locator)}
                    for source in intent.sources],
        "priorArtifacts": prior,
        "requiredOutput": manifest_stage.get("produces", []),
        "successCriteria": manifest_stage.get("successCriteria", []),
    }
    try:
        response = client.model_chat(
            model=selected.model_id,
            messages=[
                {"role": "system", "content": (
                    "You are the OpenMontage stage director. Follow the supplied director skill. "
                    "Return one bounded JSON object only. Do not include credentials, URLs, markdown fences, "
                    "or claims about work that was not performed.\n\n" + skill_text
                )},
                {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
            ],
            temperature=0.2,
            max_tokens=4096,
            response_format={"type": "json_object"},
        )
        content = response["choices"][0]["message"]["content"]
        output = json.loads(_strip_json_fence(content))
        if not isinstance(output, dict) or len(json.dumps(output, ensure_ascii=False)) > 64 * 1024:
            raise ValueError("director output invalid")
        lowered = json.dumps(output, ensure_ascii=False).lower()
        if any(marker in lowered for marker in ("authorization", "bearer ", "access_key", "apikey", "x-oss-signature")):
            raise ValueError("director output contains sensitive material")
        return {"output": output, "model": selected.model_id, "provider": selected.channel}
    except (GatewayRequestError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("stage_director_failed") from exc


def _prior_artifact_context(workspace: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    total = 0
    for path in sorted((workspace / "prior-artifacts").glob("*.json")):
        if total >= 96 * 1024:
            break
        try:
            text = path.read_text(encoding="utf-8")
            total += len(text.encode("utf-8"))
            if total <= 96 * 1024:
                result.append({"name": path.name, "artifact": json.loads(text)})
        except (OSError, ValueError):
            continue
    return result


def _strip_json_fence(value: str) -> str:
    text = value.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _video_prompt(intent: Any, scene: dict[str, Any] | None = None) -> str:
    production = intent.production
    source_modes = ", ".join(source.kind for source in intent.sources)
    subtitle_instruction = {
        "none": "Do not add subtitles or on-screen captions.",
        "clean": "Add clean, readable subtitles in the requested language.",
        "karaoke": "Add karaoke-style word-synced subtitles in the requested language.",
        "highlight": "Add subtitles in the requested language with important words highlighted.",
    }[production.subtitleStyle]
    music_instruction = {
        "none": "Do not add background music.",
        "auto": "Use a fitting background music bed if the provider supports native audio.",
        "provided": "Use the provided music source as the background music bed.",
    }[production.music]
    scene_instruction = ""
    target_duration = production.durationSeconds
    if scene is not None:
        target_duration = scene["generationDurationSeconds"]
        scene_instruction = (
            f"This is scene {scene['index']} of {scene['count']} in a {production.durationSeconds}-second sequence. "
            f"Narrative role: {scene['role']}. Make this segment visually distinct while preserving continuity. "
        )
    return (
        f"Create a concise {production.visualStyle} video for this brief: {intent.brief}. "
        f"{scene_instruction}"
        f"Use these source modes as inputs where available: {source_modes}. "
        f"Use a {production.aspectRatio} composition at {production.resolution} delivery resolution "
        f"with a target duration of {target_duration} seconds. "
        f"Narration language: {production.language}; voice direction: {production.voice}. "
        f"{subtitle_instruction} {music_instruction} "
        f"Budget tier: {production.budgetTier}. Keep motion coherent and avoid watermarks or logos."
    )


def _build_scene_plan(duration_seconds: int, max_scene_seconds: int) -> list[dict[str, Any]]:
    remaining = duration_seconds
    start = 0
    scenes: list[dict[str, Any]] = []
    while remaining > 0:
        planned = min(max_scene_seconds, remaining)
        scenes.append({
            "index": len(scenes) + 1,
            "startSeconds": start,
            "endSeconds": start + planned,
            "durationSeconds": planned,
            "generationDurationSeconds": _provider_duration(planned, max_scene_seconds),
            "role": "pending",
        })
        start += planned
        remaining -= planned
    count = len(scenes)
    for scene in scenes:
        scene["count"] = count
        scene["role"] = _scene_role(scene["index"], count)
    return scenes


def _scene_role(index: int, count: int) -> str:
    if index == 1:
        return "hook"
    if index == count:
        return "close"
    middle_roles = ("context", "development", "proof", "payoff")
    return middle_roles[min(index - 2, len(middle_roles) - 1)]


def _provider_duration(duration_seconds: int, max_scene_seconds: int) -> int:
    if max_scene_seconds == 12:
        return int(_video_seconds(duration_seconds))
    return min(max_scene_seconds, max(1, duration_seconds))


def _long_form_stage_result(intent: Any, stage: str) -> dict[str, Any]:
    plan = _build_scene_plan(intent.production.durationSeconds, 12)
    artifact_type = {
        "intake": "brief",
        "research": "research_brief",
        "proposal": "proposal_packet",
        "script": "script",
        "scene_plan": "scene_plan",
        "assets": "asset_manifest",
        "edit": "edit_decisions",
        "compose": "render_report",
    }.get(stage, "stage_output")
    return {
        "mode": "multi_scene" if len(plan) > 1 else "single_scene",
        "stage": stage,
        "artifactType": artifact_type,
        "artifactStatus": "completed",
        "requestedDurationSeconds": intent.production.durationSeconds,
        "sceneCount": len(plan),
        "scenes": plan,
        "production": intent.production.model_dump(mode="json"),
    }


def _source_inventory(intent: Any, workspace: Path) -> list[dict[str, Any]]:
    materialized: dict[str, Path] = {}
    manifest_path = workspace / "inputs" / "manifest.json"
    if manifest_path.is_file():
        try:
            entries = json.loads(manifest_path.read_text(encoding="utf-8"))
            for index, entry in enumerate(entries):
                path = Path(str(entry.get("path", "")))
                if path.is_file():
                    materialized[f"{entry.get('kind', 'source')}:{index}"] = path
        except (OSError, ValueError, TypeError):
            materialized = {}

    inventory: list[dict[str, Any]] = []
    local_index = 0
    for index, source in enumerate(intent.sources):
        local = None
        if source.objectKey:
            local = next(iter(list(materialized.values())[local_index:local_index + 1]), None)
            local_index += 1
        source_id = "source-" + hashlib.sha256(
            f"{index}:{source.kind}:{source.objectKey or source.locator or ''}".encode("utf-8")
        ).hexdigest()[:12]
        inventory.append({
            "sourceId": source_id,
            "kind": source.kind,
            "materialized": bool(local),
            "mediaType": local.suffix.lower().lstrip(".") if local else None,
            "sizeBytes": local.stat().st_size if local else source.sizeBytes,
            "externalReference": bool(source.locator),
        })
    return inventory


def _research_findings(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{
        "sourceId": source["sourceId"],
        "kind": source["kind"],
        "usable": source["kind"] == "prompt" or source["materialized"] or source["externalReference"],
        "analysis": "available_for_scene_binding" if source["kind"] != "prompt" else "creative_brief_available",
    } for source in sources]


def _production_constraints(intent: Any) -> list[dict[str, Any]]:
    production = intent.production
    return [
        {"name": "duration", "value": production.durationSeconds, "unit": "seconds"},
        {"name": "aspectRatio", "value": production.aspectRatio},
        {"name": "resolution", "value": production.resolution},
        {"name": "language", "value": production.language},
        {"name": "subtitles", "value": production.subtitleStyle},
        {"name": "music", "value": production.music},
    ]


def _estimated_cost(intent: Any) -> float:
    rate = {"economy": 0.04, "balanced": 0.08, "premium": 0.16}[intent.production.budgetTier]
    return round(max(0.01, intent.production.durationSeconds * rate), 2)


def _composition_options() -> list[str]:
    try:
        registry.discover()
        runtimes = registry.provider_menu_summary().get("composition_runtimes", {})
        available = [name for name in ("remotion", "hyperframes", "ffmpeg") if runtimes.get(name)]
        return available or ["ffmpeg"]
    except Exception:
        return ["ffmpeg"]


def _script_beats(plan: list[dict[str, Any]], narration: str) -> list[dict[str, Any]]:
    words = narration.split()
    per_scene = max(1, (len(words) + len(plan) - 1) // max(1, len(plan))) if words else 0
    beats: list[dict[str, Any]] = []
    for offset, scene in enumerate(plan):
        excerpt = " ".join(words[offset * per_scene:(offset + 1) * per_scene]) if words else None
        beats.append({
            "sceneId": f"scene-{scene['index']}",
            "startSeconds": scene["startSeconds"],
            "endSeconds": scene["endSeconds"],
            "narrationText": excerpt,
        })
    return beats


def _scene_artifacts(plan: list[dict[str, Any]], intent: Any, narration: str) -> list[dict[str, Any]]:
    beats = {beat["sceneId"]: beat for beat in _script_beats(plan, narration)}
    return [{
        **scene,
        "sceneId": f"scene-{scene['index']}",
        "visualBrief": f"{intent.brief} | {scene['role']}",
        "narrationText": beats[f"scene-{scene['index']}"]["narrationText"],
    } for scene in plan]


def _regeneration_summary(intent: Any) -> dict[str, Any] | None:
    if not intent.targetSceneId:
        return None
    return {
        "targetSceneId": intent.targetSceneId,
        "sourceRunRevision": intent.sourceRunRevision,
        "sourceSceneRevision": intent.sourceSceneRevision,
    }


def _scene_selected(intent: Any, scene_id: str) -> bool:
    return intent.targetSceneId is None or intent.targetSceneId == scene_id


def _generate_sora_scenes(model: str, intent: Any, scene_plan: list[dict[str, Any]],
                          output: Path, reference_path: str | None) -> None:
    scene_dir = output.parent / "scenes"
    scene_dir.mkdir(parents=True, exist_ok=True)
    scene_paths: list[Path] = []
    for scene in scene_plan:
        scene_id = f"scene-{scene['index']}"
        scene_output = scene_dir / f"{scene_id}.mp4"
        if not _scene_selected(intent, scene_id):
            prior = output.parent.parent / "prior-scenes" / scene_output.name
            if not prior.is_file():
                raise RuntimeError("prior_scene_artifact_missing")
            shutil.copy2(prior, scene_output)
            scene_paths.append(scene_output)
            continue
        video_inputs: dict[str, Any] = {
            "prompt": _video_prompt(intent, scene),
            "model": model,
            "size": _video_size(intent.production.aspectRatio),
            "seconds": str(scene["generationDurationSeconds"]),
            "output_path": str(scene_output),
        }
        if reference_path:
            video_inputs.update({"operation": "image_to_video", "input_reference_path": reference_path})
        result = SoraVideo().execute(video_inputs)
        if not result.success:
            raise RuntimeError("video_generation_failed")
        scene_paths.append(scene_output)
    if len(scene_paths) > 1:
        _concatenate_scenes(scene_paths, output)
    elif scene_paths:
        shutil.copy2(scene_paths[0], output)


def _concatenate_scenes(scene_paths: list[Path], output: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("scene_stitcher_unavailable")
    manifest = scene_paths[0].parent / "concat.txt"
    manifest.write_text("".join(f"file '{path.name}'\n" for path in scene_paths), encoding="utf-8")
    completed = subprocess.run(
        [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", manifest.name,
         "-c", "copy", "-movflags", "+faststart", str(output)],
        cwd=manifest.parent, check=False, capture_output=True, text=True, timeout=900,
    )
    if completed.returncode != 0 or not output.is_file() or output.stat().st_size <= 0:
        raise RuntimeError("scene_stitch_failed")


def _reference_image(workspace: Path) -> str | None:
    manifest = workspace / "inputs" / "manifest.json"
    if not manifest.is_file():
        return None
    try:
        entries = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and entry.get("kind") == "image" and Path(str(entry.get("path", ""))).is_file():
            return str(entry["path"])
    return None


def _video_size(aspect_ratio: str) -> str:
    return "720x1280" if aspect_ratio == "9:16" else "1280x720"


def _video_seconds(duration_seconds: int) -> str:
    if duration_seconds <= 4:
        return "4"
    if duration_seconds <= 8:
        return "8"
    return "12"


def _enforce_budget_policy(intent: Any, model: str) -> None:
    """Reject combinations that cannot be honestly delivered within the tier."""

    production = intent.production
    if production.budgetTier == "economy" and production.resolution == "4k":
        raise RuntimeError("budget_resolution_conflict")
    if production.budgetTier == "economy" and model == "sora-2-pro":
        raise RuntimeError("budget_model_conflict")


def _apply_production_contract(output: Path, intent: Any, workspace: Path) -> dict[str, Any]:
    """Make user-visible production settings true in the final MP4."""

    production = intent.production
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("production_normalizer_unavailable")

    target_width, target_height = _target_dimensions(
        production.aspectRatio, production.resolution
    )
    target_duration = float(production.durationSeconds)
    source_probe = _probe_video(output, ffprobe)
    source_is_short = source_probe["durationSeconds"] + 0.05 < target_duration
    narration = _generate_narration(intent, workspace) if (intent.narrationText or "").strip() else None
    music = _resolve_music_source(intent, workspace)
    subtitle = _write_subtitle_asset(intent, workspace, target_duration)
    normalized = output.with_name(f"{output.stem}.normalized{output.suffix}")
    video_filter = (
        f"scale={target_width}:{target_height}:force_original_aspect_ratio=increase,"
        f"crop={target_width}:{target_height},"
        f"trim=duration={target_duration},setpts=PTS-STARTPTS"
    )
    if subtitle:
        video_filter = f"{video_filter},{_subtitle_filter(subtitle)}"

    # Provider clips are often shorter than the requested delivery duration.
    # Loop the source in that case; padding with tpad would freeze the last
    # frame for the rest of the output.
    inputs = (["-stream_loop", "-1"] if source_is_short else []) + ["-i", str(output)]
    filter_parts = [f"[0:v]{video_filter}[v]"]
    audio_maps: list[str] = []
    if narration:
        inputs.extend(["-i", str(narration)])
        filter_parts.append(
            f"[1:a]apad=pad_dur={target_duration},atrim=duration={target_duration},"
            "asetpts=PTS-STARTPTS[narration]"
        )
        audio_maps.append("[narration]")

    if production.music == "provided":
        if not music:
            raise RuntimeError("provided_music_missing")
        inputs.extend(["-i", str(music)])
        music_index = 2 if narration else 1
        filter_parts.append(
            f"[{music_index}:a]aloop=loop=-1:size=2e+09,atrim=duration={target_duration},"
            "asetpts=PTS-STARTPTS[music]"
        )
        audio_maps.append("[music]")
    elif production.music == "auto" and source_probe["hasAudio"]:
        # Native provider audio is the auto music source. It is mixed with TTS
        # when present, while the explicit none option strips it entirely.
        filter_parts.append(
            f"[0:a]apad=pad_dur={target_duration},atrim=duration={target_duration},"
            "asetpts=PTS-STARTPTS[native]"
        )
        audio_maps.append("[native]")

    if len(audio_maps) > 1:
        filter_parts.append(
            "".join(audio_maps)
            + f"amix=inputs={len(audio_maps)}:duration=longest:dropout_transition=0,"
            "aresample=async=1[a]"
        )
        audio_maps = ["[a]"]

    # Worker pods intentionally run at the minimum memory profile. Limit
    # encoder/filter parallelism so a 1080p portrait render does not get OOM
    # killed while still producing the requested dimensions.
    command = [
        ffmpeg,
        "-y",
        "-threads",
        "1",
        "-filter_threads",
        "1",
        "-filter_complex_threads",
        "1",
        *inputs,
        "-filter_complex",
        ";".join(filter_parts),
        "-map",
        "[v]",
    ]
    if audio_maps:
        command.extend(["-map", audio_maps[0]])
    else:
        command.extend(["-an"])
    command.extend(
        [
            "-t",
            str(production.durationSeconds),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(normalized),
        ]
    )
    completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=900)
    if completed.returncode != 0 or not normalized.is_file() or normalized.stat().st_size <= 0:
        raise RuntimeError("production_normalization_failed")
    os.replace(normalized, output)
    actual = _probe_video(output, ffprobe)
    if (
        abs(actual["durationSeconds"] - target_duration) > 0.15
        or actual["width"] != target_width
        or actual["height"] != target_height
    ):
        raise RuntimeError("production_contract_mismatch")
    actual.update(
        {
            "sourceKinds": [source.kind for source in intent.sources],
            "narrationGenerated": narration is not None,
            "narrationModel": "tts-1" if narration else None,
            "narrationVoice": _tts_voice(production.voice) if narration else None,
            "subtitleEmbedded": subtitle is not None,
            "musicApplied": production.music,
        }
    )
    return {
        "requested": {
            "sourceKinds": [source.kind for source in intent.sources],
            "durationSeconds": production.durationSeconds,
            "aspectRatio": production.aspectRatio,
            "resolution": production.resolution,
            "language": production.language,
            "voice": production.voice,
            "subtitleStyle": production.subtitleStyle,
            "music": production.music,
            "visualStyle": production.visualStyle,
            "budgetTier": production.budgetTier,
        },
        "actual": actual,
    }


def _target_dimensions(aspect_ratio: str, resolution: str) -> tuple[int, int]:
    short_edge = {"720p": 720, "1080p": 1080, "4k": 2160}[resolution]
    if aspect_ratio == "9:16":
        return short_edge, round(short_edge * 16 / 9)
    if aspect_ratio == "1:1":
        return short_edge, short_edge
    return round(short_edge * 16 / 9), short_edge


def _probe_video(path: Path, ffprobe: str) -> dict[str, Any]:
    completed = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
         "-of", "json", str(path)],
        check=False, capture_output=True, text=True, timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError("publish_artifact_invalid")
    try:
        payload = json.loads(completed.stdout or "{}")
        stream = next(item for item in payload.get("streams", []) if item.get("codec_type") == "video")
        return {
            "durationSeconds": round(float(payload.get("format", {}).get("duration") or 0), 3),
            "width": int(stream["width"]),
            "height": int(stream["height"]),
            "hasAudio": any(item.get("codec_type") == "audio" for item in payload.get("streams", [])),
        }
    except (KeyError, TypeError, ValueError, StopIteration) as exc:
        raise RuntimeError("publish_artifact_invalid") from exc


def _generate_narration(intent: Any, workspace: Path) -> Path:
    narration_text = (intent.narrationText or "").strip()
    if not narration_text:
        raise RuntimeError("narration_text_missing")
    if not gateway_configured():
        raise RuntimeError("narration_unavailable")
    voice = _tts_voice(intent.production.voice)
    language = intent.production.language.lower()
    language_type = "English" if language.startswith("en") else "Chinese" if language.startswith("zh") else "Auto"
    try:
        body = GatewayClient().model_speech(
            # The test gateway currently exposes Qwen TTS in /v1/models but
            # its native conversion route is not implemented.  tts-1 is the
            # verified OpenAI-compatible route and remains gateway-bound.
            model="tts-1",
            input=narration_text[:600],
            voice=voice,
            response_format="mp3",
        )
    except GatewayRequestError as exc:
        raise RuntimeError("narration_generation_failed") from exc
    path = workspace / "audio" / "narration.mp3"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    if not path.stat().st_size:
        raise RuntimeError("narration_generation_failed")
    return path


def _tts_voice(voice: str) -> str:
    return {"female-warm": "nova", "male-calm": "onyx", "neutral": "alloy"}.get(voice, "alloy")


def _resolve_music_source(intent: Any, workspace: Path) -> Path | None:
    if intent.production.music != "provided":
        return None
    manifest = workspace / "inputs" / "manifest.json"
    if not manifest.is_file():
        return None
    try:
        entries = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for entry in entries if isinstance(entries, list) else []:
        path = Path(str(entry.get("path", ""))) if isinstance(entry, dict) else Path()
        if isinstance(entry, dict) and entry.get("kind") == "audio" and path.is_file():
            return path
    return None


def _write_subtitle_asset(intent: Any, workspace: Path, duration: float) -> Path | None:
    style = intent.production.subtitleStyle
    if style == "none":
        return None
    words = re.findall(r"\S+", intent.brief.strip())
    if not words:
        raise RuntimeError("subtitle_text_missing")
    path = workspace / "captions" / ("captions.ass" if style == "karaoke" else "captions.srt")
    path.parent.mkdir(parents=True, exist_ok=True)
    if style == "karaoke":
        _write_ass(path, words, duration)
    else:
        path.write_text(_render_srt(words, duration), encoding="utf-8")
    return path


def _render_srt(words: list[str], duration: float) -> str:
    chunks = [words[index:index + 8] for index in range(0, len(words), 8)]
    lines: list[str] = []
    for index, chunk in enumerate(chunks, 1):
        start = duration * index / len(chunks) - duration / len(chunks)
        end = duration * index / len(chunks)
        lines.extend([str(index), f"{_srt_time(start)} --> {_srt_time(end)}", " ".join(chunk), ""])
    return "\n".join(lines)


def _write_ass(path: Path, words: list[str], duration: float) -> None:
    centiseconds = max(1, round(duration * 100 / len(words)))
    karaoke = "".join(f"{{\\k{centiseconds}}}{word} " for word in words).strip()
    end = _ass_time(duration)
    path.write_text(
        "[Script Info]\nScriptType: v4.00+\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Arial,42,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,2,1,2,40,40,60,1\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        f"Dialogue: 0,0:00:00.00,{end},Default,,0,0,0,,{karaoke}\n",
        encoding="utf-8",
    )


def _srt_time(seconds: float) -> str:
    total_ms = max(0, round(seconds * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _ass_time(seconds: float) -> str:
    total_cs = max(0, round(seconds * 100))
    hours, remainder = divmod(total_cs, 360000)
    minutes, remainder = divmod(remainder, 6000)
    secs, centis = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def _subtitle_filter(path: Path) -> str:
    escaped = str(path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    suffix = path.suffix.lower()
    return f"{'ass' if suffix == '.ass' else 'subtitles'}='{escaped}'"


def _generate_gateway_scenes(model: str, intent: Any, scene_plan: list[dict[str, Any]],
                             output: Path, workspace: Path | None = None) -> tuple[str, str]:
    scene_dir = output.parent / "scenes"
    scene_dir.mkdir(parents=True, exist_ok=True)
    scene_paths: list[Path] = []
    provider = route = ""
    for scene in scene_plan:
        scene_id = f"scene-{scene['index']}"
        scene_output = scene_dir / f"{scene_id}.mp4"
        if not _scene_selected(intent, scene_id):
            prior = output.parent.parent / "prior-scenes" / scene_output.name
            if not prior.is_file():
                raise RuntimeError("prior_scene_artifact_missing")
            shutil.copy2(prior, scene_output)
            scene_paths.append(scene_output)
            continue
        provider, route = _generate_gateway_video(
            model, intent, scene_output, workspace, scene=scene,
        )
        scene_paths.append(scene_output)
    if len(scene_paths) > 1:
        _concatenate_scenes(scene_paths, output)
    elif scene_paths:
        shutil.copy2(scene_paths[0], output)
    return provider, route


def _generate_gateway_video(model: str, intent: Any, output: Path, workspace: Path | None = None,
                            scene: dict[str, Any] | None = None) -> tuple[str, str]:
    client = GatewayClient()
    item: GatewayModel = client.resolve_model(model, "video_generation")
    prompt = _video_prompt(intent, scene)
    duration = scene["generationDurationSeconds"] if scene else min(intent.production.durationSeconds, 10)
    reference_path = _reference_image(workspace) if workspace else None
    if reference_path and "image" not in item.input_types:
        raise GatewayRequestError(f"gateway model does not support image references: {model}")
    if item.protocol == "byteplus-task":
        payload: dict[str, Any] = {
            "model": model,
            "content": [{"type": "text", "text": prompt}],
            "ratio": intent.production.aspectRatio,
            "duration": duration,
            "generate_audio": True,
            "watermark": False,
        }
        if reference_path:
            payload["content"].append({"type": "image_url", "image_url": {"url": _data_uri(reference_path)}})
    else:
        payload = {
            "model": model,
            "input": {"prompt": prompt},
            "parameters": {
                "duration": duration,
                "resolution": intent.production.resolution.upper(),
                "watermark": False,
                "audio": True,
            },
        }
        if reference_path:
            payload["input"]["media"] = [{"type": "first_frame", "url": _data_uri(reference_path)}]
    submitted = client.submit_task(model=model, capability="video_generation", payload=payload)
    output_body = submitted.get("output")
    output_task_id = output_body.get("task_id") if isinstance(output_body, dict) else None
    task_id = str(submitted.get("id") or submitted.get("task_id") or output_task_id or "").strip()
    if not task_id:
        raise GatewayRequestError("gateway video submit returned no task id")
    terminal = client.poll_task_until_terminal(
        model=model,
        capability="video_generation",
        task_id=task_id,
        timeout=900.0,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    client.download_task_result(terminal, str(output))
    return item.channel, item.submit_path


def _data_uri(path: str) -> str:
    import base64
    import mimetypes
    mime = mimetypes.guess_type(path)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(Path(path).read_bytes()).decode('ascii')}"


def _verify_video(path: Path) -> None:
    if not path.is_file() or path.stat().st_size <= 0:
        raise RuntimeError("publish_artifact_missing")
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type",
                "-of",
                "json",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        payload = json.loads(completed.stdout or "{}")
        duration = float(payload.get("format", {}).get("duration") or 0)
        has_video = any(stream.get("codec_type") == "video" for stream in payload.get("streams", []))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise RuntimeError("publish_artifact_invalid") from exc
    if completed.returncode != 0 or not has_video or duration <= 0:
        raise RuntimeError("publish_artifact_invalid")


def _encode(value: dict[str, Any]) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
