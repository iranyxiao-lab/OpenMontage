from __future__ import annotations

import json
import os
import re
import shutil
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
        _enforce_budget_policy(intent, model)
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

    actual = _apply_production_contract(output, intent, workspace)
    checkpoint["result"] = {
        "name": output.name,
        "contentType": "video/mp4",
        "provider": provider,
        "model": model,
        "route": route,
        "production": actual,
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
    narration = _generate_narration(intent, workspace)
    music = _resolve_music_source(intent, workspace)
    subtitle = _write_subtitle_asset(intent, workspace, target_duration)
    normalized = output.with_name(f"{output.stem}.normalized{output.suffix}")
    video_filter = (
        f"scale={target_width}:{target_height}:force_original_aspect_ratio=increase,"
        f"crop={target_width}:{target_height},"
        f"tpad=stop_mode=clone:stop_duration={target_duration},"
        f"trim=duration={target_duration},setpts=PTS-STARTPTS"
    )
    if subtitle:
        video_filter = f"{video_filter},{_subtitle_filter(subtitle)}"

    inputs = ["-i", str(output)]
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

    command = [ffmpeg, "-y", *inputs, "-filter_complex", ";".join(filter_parts), "-map", "[v]"]
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
            "veryfast",
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
            "narrationModel": "qwen3-tts-flash" if narration else None,
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
    if not intent.brief.strip():
        raise RuntimeError("narration_text_missing")
    if not gateway_configured():
        raise RuntimeError("narration_unavailable")
    voice = _tts_voice(intent.production.voice)
    language = intent.production.language.lower()
    language_type = "English" if language.startswith("en") else "Chinese" if language.startswith("zh") else "Auto"
    try:
        body = GatewayClient().model_speech(
            model="qwen3-tts-flash",
            input=intent.brief[:600],
            voice=voice,
            language_type=language_type,
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
    return {"female-warm": "Cherry", "male-calm": "Ethan", "neutral": "Cherry"}.get(voice, "Cherry")


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
