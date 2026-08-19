from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from tools.video.sora_video import SoraVideo


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
    model = os.getenv("OPENMONTAGE_VIDEO_MODEL", "sora-2").strip() or "sora-2"
    result = SoraVideo().execute({
        "prompt": _video_prompt(intent),
        "model": model,
        "size": _video_size(intent.production.aspectRatio),
        "seconds": _video_seconds(intent.production.durationSeconds),
        "output_path": str(output),
    })
    if not result.success:
        raise RuntimeError("video_generation_failed")

    _verify_video(output)
    checkpoint["result"] = {
        "name": output.name,
        "contentType": "video/mp4",
        "provider": "openai",
        "model": model,
        "route": "/v1/videos",
    }
    return _encode(checkpoint)


def _video_prompt(intent: Any) -> str:
    production = intent.production
    return (
        f"Create a concise {production.visualStyle} video for this brief: {intent.brief}. "
        f"Use a {production.aspectRatio} composition. Keep motion coherent and avoid watermarks, "
        "logos, captions, and on-screen text."
    )


def _video_size(aspect_ratio: str) -> str:
    return "720x1280" if aspect_ratio == "9:16" else "1280x720"


def _video_seconds(duration_seconds: int) -> str:
    if duration_seconds <= 4:
        return "4"
    if duration_seconds <= 8:
        return "8"
    return "12"


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
