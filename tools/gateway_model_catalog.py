"""Model and protocol registry for the internal AI gateway.

The gateway model list is only a visibility check.  Capability and route are
kept here because an OpenAI-compatible model listing does not describe the
provider protocol or whether a model is an image/video/task endpoint.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class GatewayModel:
    model_id: str
    channel: str
    capability: str
    protocol: str
    submit_path: str
    query_path: str | None = None
    cancel_path: str | None = None
    input_types: tuple[str, ...] = ("text",)
    cancel_method: str = "DELETE"
    required_input_types: tuple[str, ...] = ("text",)


BYTEPLUS_CHAT = (
    "dola-seed-2-1-turbo-260628",
    "seed-2-0-lite-260228",
)
BYTEPLUS_IMAGE = ("seedream-4-5-251128",)
BYTEPLUS_VIDEO = ("dreamina-seedance-2-0-fast-260128",)

BAILIAN_TEXT = (
    "deepseek-v4-flash-0731", "qvq-max", "qwen-coder-plus", "qwen-flash",
    "qwen-max", "qwen-mt-plus", "qwen-mt-turbo", "qwen-omni-turbo",
    "qwen-plus", "qwen-plus-character", "qwen-plus-latest", "qwen-turbo",
    "qwen-vl-max", "qwen-vl-plus", "qwen3-14b", "qwen3-235b-a22b",
    "qwen3-235b-a22b-instruct-2507", "qwen3-235b-a22b-thinking-2507",
    "qwen3-30b-a3b", "qwen3-30b-a3b-instruct-2507",
    "qwen3-30b-a3b-thinking-2507", "qwen3-32b", "qwen3-8b",
    "qwen3-coder-480b-a35b-instruct", "qwen3-coder-flash", "qwen3-coder-next",
    "qwen3-coder-plus", "qwen3-coder-plus-2025-07-22", "qwen3-coder-plus-2025-09-23",
    "qwen3-max", "qwen3-max-2025-09-23", "qwen3-max-2026-01-23", "qwen3-max-preview",
    "qwen3-next-80b-a3b-instruct", "qwen3-next-80b-a3b-thinking",
    "qwen3-livetranslate-flash-realtime", "qwen3-omni-flash", "qwen3-omni-flash-realtime",
    "qwen3-vl-235b-a22b-instruct", "qwen3-vl-235b-a22b-thinking", "qwen3-vl-flash",
    "qwen3-vl-plus", "qwen3-vl-plus-2025-09-23", "qwen3.5-122b-a10b", "qwen3.5-27b",
    "qwen3.5-35b-a3b", "qwen3.5-397b-a17b", "qwen3.5-flash", "qwen3.5-omni-flash",
    "qwen3.5-omni-flash-2026-03-15", "qwen3.5-omni-plus", "qwen3.5-omni-plus-2026-03-15",
    "qwen3.5-plus", "qwen3.6-27b", "qwen3.6-35b-a3b", "qwen3.6-flash",
    "qwen3.6-max-preview", "qwen3.6-plus", "qwen3.7-flash", "qwen3.7-max",
    "qwen3.7-plus", "qwen3.8-max", "qwq-plus",
)
BAILIAN_IMAGE = ("qwen-image-3.0-pro", "qwen-image-plus")
BAILIAN_IMAGE_EDIT = ("qwen-image-edit",)
BAILIAN_TTS = (
    "qwen3-tts-flash", "qwen3-tts-flash-2025-09-18",
    "qwen3-tts-flash-realtime", "qwen3-tts-flash-realtime-2025-09-18",
)
BAILIAN_VIDEO = (
    "wan2.2-i2v-flash", "wan2.2-i2v-plus", "wan2.5-i2v-preview", "wan2.7-i2v",
    "wan2.7-t2v", "wan3.0-video-prime",
)
BAILIAN_IMAGE_VIDEO = ("wan2.7-image", "wan2.7-image-pro")
BAILIAN_STT = ("fun-asr",)
BAILIAN_EMBEDDING = ("text-embedding-v3", "text-embedding-v4")
OPENAI_TTS = ("tts-1", "tts-1-hd", "gpt-4o-mini-tts")


def _entries() -> tuple[GatewayModel, ...]:
    entries: list[GatewayModel] = []
    for model in BYTEPLUS_CHAT:
        entries.append(GatewayModel(model, "byteplus-modelark", "chat", "byteplus-official",
                                    "/byteplus/api/v3/chat/completions", input_types=("text", "image")))
    for model in BYTEPLUS_IMAGE:
        entries.append(GatewayModel(model, "byteplus-modelark", "image_generation", "byteplus-official",
                                    "/byteplus/api/v3/images/generations", input_types=("text", "image")))
    for model in BYTEPLUS_VIDEO:
        entries.append(GatewayModel(model, "byteplus-modelark", "video_generation", "byteplus-task",
                                    "/byteplus/api/v3/contents/generations/tasks",
                                    "/byteplus/api/v3/contents/generations/tasks/{task_id}",
                                    "/byteplus/api/v3/contents/generations/tasks/{task_id}",
                                    ("text", "image", "video", "audio")))
    for model in BAILIAN_TEXT:
        entries.append(GatewayModel(model, "ali", "chat", "openai-compatible", "/v1/chat/completions",
                                    input_types=("text", "image") if "vl" in model or "omni" in model else ("text",)))
    for model in BAILIAN_IMAGE:
        entries.append(GatewayModel(model, "ali", "image_generation", "bailian-native",
                                    "/api/v1/services/aigc/image-generation/generation",
                                    "/api/v1/tasks/{task_id}", "/api/v1/tasks/{task_id}/cancel",
                                    input_types=("text", "image"), cancel_method="POST"))
    for model in BAILIAN_IMAGE_EDIT:
        entries.append(GatewayModel(model, "ali", "image_generation", "openai-multipart",
                                    "/v1/images/edits", input_types=("text", "image")))
    for model in BAILIAN_TTS:
        entries.append(GatewayModel(model, "ali", "tts", "openai-compatible", "/v1/audio/speech",
                                    input_types=("text",)))
    for model in BAILIAN_VIDEO:
        entries.append(GatewayModel(model, "ali", "video_generation", "bailian-task",
                                    "/api/v1/services/aigc/video-generation/video-synthesis",
                                    "/api/v1/tasks/{task_id}", None,
                                    ("text", "image", "audio"), "DELETE",
                                    ("image",) if "-i2v" in model else ("text",)))
    for model in BAILIAN_IMAGE_VIDEO:
        entries.append(GatewayModel(model, "ali", "image_generation", "bailian-task",
                                    "/api/v1/services/aigc/image-generation/generation",
                                    "/api/v1/tasks/{task_id}", "/api/v1/tasks/{task_id}/cancel",
                                    ("text", "image"), "POST"))
    for model in BAILIAN_STT:
        entries.append(GatewayModel(model, "ali", "stt", "bailian-task",
                                    "/api/v1/services/audio/asr/transcription", "/api/v1/tasks/{task_id}",
                                    None, input_types=("audio",)))
    for model in BAILIAN_EMBEDDING:
        entries.append(GatewayModel(model, "ali", "embedding", "openai-compatible", "/v1/embeddings",
                                    input_types=("text",)))
    for model in OPENAI_TTS:
        entries.append(GatewayModel(model, "openai", "tts", "openai-compatible", "/v1/audio/speech",
                                    input_types=("text",)))
    return tuple(entries)


KNOWN_MODELS = {item.model_id: item for item in _entries()}


def _override_entries() -> dict[str, GatewayModel]:
    raw = os.environ.get("OPENMONTAGE_GATEWAY_MODEL_OVERRIDES", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("OPENMONTAGE_GATEWAY_MODEL_OVERRIDES must be valid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("OPENMONTAGE_GATEWAY_MODEL_OVERRIDES must be an object")
    result: dict[str, GatewayModel] = {}
    for model_id, value in data.items():
        if model_id not in KNOWN_MODELS or not isinstance(value, dict):
            continue
        base = KNOWN_MODELS[model_id]
        result[model_id] = GatewayModel(
            model_id=model_id,
            channel=str(value.get("channel", base.channel)),
            capability=str(value.get("capability", base.capability)),
            protocol=str(value.get("protocol", base.protocol)),
            submit_path=str(value.get("submit_path", base.submit_path)),
            query_path=value.get("query_path", base.query_path),
            cancel_path=value.get("cancel_path", base.cancel_path),
            cancel_method=str(value.get("cancel_method", base.cancel_method)),
            input_types=tuple(value.get("input_types", base.input_types)),
            required_input_types=tuple(value.get("required_input_types", base.required_input_types)),
        )
    return result


def model_catalog() -> dict[str, GatewayModel]:
    catalog = dict(KNOWN_MODELS)
    catalog.update(_override_entries())
    return catalog


def visible_models(model_records: Iterable[dict[str, Any]]) -> dict[str, GatewayModel]:
    """Return registered models that are currently exposed by the gateway."""

    catalog = model_catalog()
    visible: dict[str, GatewayModel] = {}
    for record in model_records:
        if not isinstance(record, dict):
            continue
        model_id = str(record.get("id", "")).strip()
        owner = str(record.get("owned_by", "")).strip()
        item = catalog.get(model_id)
        endpoint_types = record.get("supported_endpoint_types")
        if item is not None and owner == item.channel and _supports_protocol(item, endpoint_types):
            visible[model_id] = item
    return visible


def _supports_protocol(item: GatewayModel, endpoint_types: Any) -> bool:
    if endpoint_types is None:
        return True
    if not isinstance(endpoint_types, (list, tuple, set)):
        return False
    advertised = {str(value) for value in endpoint_types}
    accepted = {
        "openai-compatible": {"openai"},
        "openai-multipart": {"openai"},
        "byteplus-official": {"openai", "byteplus-openai"},
        "byteplus-task": {"byteplus-seedance", "configurable-task"},
        "bailian-native": {"openai", "configurable-task"},
        "bailian-task": {"openai", "legacy-video", "configurable-task"},
    }.get(item.protocol, set())
    return bool(advertised & accepted)


def require_model(model_id: str, capability: str, visible: dict[str, GatewayModel]) -> GatewayModel:
    item = visible.get(model_id)
    if item is None:
        raise ValueError(f"gateway model is unavailable: {model_id}")
    if item.capability != capability:
        raise ValueError(f"gateway model {model_id} does not support {capability}")
    return item


def expected_model_count() -> int:
    return len(KNOWN_MODELS)
