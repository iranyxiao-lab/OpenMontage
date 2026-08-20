from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from tools.video.sora_video import SoraVideo
from tools.gateway_client import GatewayClient, GatewayRequestError, gateway_configured
from tools.gateway_model_catalog import GatewayModel


_STAGES = {
    "intake",
    "research",
    "proposal",
    "script",
    "scene_plan",
    "assets",
    "edit",
    "compose",
    "publish",
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
        return _encode(checkpoint)

    intent = command.userIntent
    if intent is None:
        raise RuntimeError("publish_user_intent_missing")

    output = workspace / "renders" / "final.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    model = (intent.production.model or os.getenv("OPENMONTAGE_VIDEO_MODEL", "sora-2")).strip() or "sora-2"
    reference_path = _reference_image(workspace)
    if model in {"sora-2", "sora-2-pro"}:
        video_inputs: dict[str, Any] = {
            "prompt": _video_prompt(intent),
            "model": model,
            "size": _video_size(intent.production.aspectRatio),
            "seconds": _video_seconds(intent.production.durationSeconds),
            "output_path": str(output),
        }
        if reference_path:
            video_inputs.update({"operation": "image_to_video", "input_reference_path": reference_path})
        result = SoraVideo().execute(video_inputs)
        if not result.success:
            raise RuntimeError("video_generation_failed")
        provider = "openai"
        route = "/v1/videos"
    else:
        if not gateway_configured():
            raise RuntimeError("gateway_configuration_missing")
        try:
            provider, route = _generate_gateway_video(model, intent, output, workspace)
        except GatewayRequestError as exc:
            raise RuntimeError("gateway_video_generation_failed") from exc

    _verify_video(output)
    checkpoint["result"] = {
        "name": output.name,
        "contentType": "video/mp4",
        "provider": provider,
        "model": model,
        "route": route,
    }
    return _encode(checkpoint)


def _video_prompt(intent: Any) -> str:
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
    return (
        f"Create a concise {production.visualStyle} video for this brief: {intent.brief}. "
        f"Use these source modes as inputs where available: {source_modes}. "
        f"Use a {production.aspectRatio} composition at {production.resolution} delivery resolution "
        f"with a target duration of {production.durationSeconds} seconds. "
        f"Narration language: {production.language}; voice direction: {production.voice}. "
        f"{subtitle_instruction} {music_instruction} "
        f"Budget tier: {production.budgetTier}. Keep motion coherent and avoid watermarks or logos."
    )


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


def _generate_gateway_video(model: str, intent: Any, output: Path, workspace: Path | None = None) -> tuple[str, str]:
    client = GatewayClient()
    item: GatewayModel = client.resolve_model(model, "video_generation")
    prompt = _video_prompt(intent)
    reference_path = _reference_image(workspace) if workspace else None
    if reference_path and "image" not in item.input_types:
        raise GatewayRequestError(f"gateway model does not support image references: {model}")
    if item.protocol == "byteplus-task":
        payload: dict[str, Any] = {
            "model": model,
            "content": [{"type": "text", "text": prompt}],
            "ratio": intent.production.aspectRatio,
            "duration": min(intent.production.durationSeconds, 12),
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
                "duration": min(intent.production.durationSeconds, 10),
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
