from __future__ import annotations

import pytest

from tools.gateway_model_catalog import (
    BYTEPLUS_CHAT,
    BYTEPLUS_IMAGE,
    BYTEPLUS_VIDEO,
    KNOWN_MODELS,
    model_catalog,
    require_model,
    visible_models,
)


def test_registry_contains_current_byteplus_and_bailian_models():
    catalog = model_catalog()
    assert set(BYTEPLUS_CHAT + BYTEPLUS_IMAGE + BYTEPLUS_VIDEO) <= set(catalog)
    assert sum(item.channel == "byteplus-modelark" for item in catalog.values()) == 4
    assert sum(item.channel == "ali" for item in catalog.values()) == 81
    assert catalog["wan3.0-video-prime"].capability == "video_generation"
    # The gateway also exposes a small set of OpenAI-compatible TTS models
    # used by the verified narration route. Keep this compatibility set
    # explicit while allowing the channel registries to evolve independently.
    assert {model_id for model_id, item in catalog.items() if item.channel == "openai"} == {
        "tts-1", "tts-1-hd", "gpt-4o-mini-tts"
    }


def test_visibility_requires_gateway_owner_and_does_not_trust_unknown_models():
    records = [
        {"id": "seedream-4-5-251128", "owned_by": "byteplus-modelark"},
        {"id": "qwen-plus", "owned_by": "ali"},
        {"id": "qwen-plus", "owned_by": "byteplus-modelark"},
        {"id": "unknown-model", "owned_by": "ali"},
        {"id": "qwen-max", "owned_by": "ali", "supported_endpoint_types": ["responses"]},
    ]
    visible = visible_models(records)
    assert set(visible) == {"seedream-4-5-251128", "qwen-plus"}
    assert visible["qwen-plus"].channel == "ali"


def test_native_video_endpoint_types_are_visible_only_for_matching_protocols():
    records = [
        {"id": "dreamina-seedance-2-0-fast-260128", "owned_by": "byteplus-modelark",
         "supported_endpoint_types": ["configurable-task", "byteplus-seedance"]},
        {"id": "wan2.7-t2v", "owned_by": "ali",
         "supported_endpoint_types": ["configurable-task", "legacy-video"]},
        {"id": "qwen-plus", "owned_by": "ali", "supported_endpoint_types": ["legacy-video"]},
    ]
    visible = visible_models(records)
    assert set(visible) == {"dreamina-seedance-2-0-fast-260128", "wan2.7-t2v"}


def test_protocol_routes_are_explicit():
    byteplus_video = KNOWN_MODELS["dreamina-seedance-2-0-fast-260128"]
    assert byteplus_video.submit_path.startswith("/byteplus/api/v3/")
    assert byteplus_video.query_path.endswith("/{task_id}")
    assert byteplus_video.cancel_path == byteplus_video.query_path

    bailian_image = KNOWN_MODELS["qwen-image-3.0-pro"]
    assert bailian_image.protocol == "bailian-native"
    assert bailian_image.submit_path == "/api/v1/services/aigc/image-generation/generation"
    bailian_edit = KNOWN_MODELS["qwen-image-edit"]
    assert bailian_edit.protocol == "openai-multipart"
    assert bailian_edit.submit_path == "/v1/images/edits"
    assert KNOWN_MODELS["wan2.7-i2v"].cancel_path is None
    assert KNOWN_MODELS["wan2.7-i2v"].required_input_types == ("image",)
    assert KNOWN_MODELS["wan2.7-t2v"].required_input_types == ("text",)
    assert KNOWN_MODELS["fun-asr"].query_path == "/api/v1/tasks/{task_id}"
    assert KNOWN_MODELS["qwen-image-3.0-pro"].query_path == "/api/v1/tasks/{task_id}"
    assert KNOWN_MODELS["qwen-image-3.0-pro"].cancel_method == "POST"
    assert KNOWN_MODELS["wan2.7-image"].cancel_method == "POST"


def test_require_model_fails_closed_for_missing_or_wrong_capability():
    visible = visible_models([{"id": "qwen-plus", "owned_by": "ali"}])
    with pytest.raises(ValueError, match="unavailable"):
        require_model("qwen-max", "chat", visible)
    with pytest.raises(ValueError, match="does not support"):
        require_model("qwen-plus", "image_generation", visible)


def test_model_readiness_override_is_preserved_in_runtime_catalog(monkeypatch):
    monkeypatch.setenv(
        "OPENMONTAGE_GATEWAY_MODEL_OVERRIDES",
        '{"dreamina-seedance-2-0-fast-260128": {"status": "DISABLED", "reason_code": "gateway_task_query_unavailable"}}',
    )
    item = model_catalog()["dreamina-seedance-2-0-fast-260128"]
    assert item.status == "DISABLED"
    assert item.reason_code == "gateway_task_query_unavailable"
    with pytest.raises(ValueError, match="not ready"):
        require_model(item.model_id, "video_generation", {item.model_id: item})
