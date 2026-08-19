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


def test_registry_contains_the_four_byteplus_and_eighty_bailian_models():
    catalog = model_catalog()
    assert len(catalog) == 84
    assert set(BYTEPLUS_CHAT + BYTEPLUS_IMAGE + BYTEPLUS_VIDEO) <= set(catalog)
    assert sum(item.channel == "byteplus-modelark" for item in catalog.values()) == 4
    assert sum(item.channel == "ali" for item in catalog.values()) == 80


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


def test_protocol_routes_are_explicit():
    byteplus_video = KNOWN_MODELS["dreamina-seedance-2-0-fast-260128"]
    assert byteplus_video.submit_path.startswith("/byteplus/api/v3/")
    assert byteplus_video.query_path.endswith("/{task_id}")
    assert byteplus_video.cancel_path == byteplus_video.query_path

    bailian_image = KNOWN_MODELS["qwen-image-3.0-pro"]
    assert bailian_image.protocol == "bailian-native"
    assert bailian_image.submit_path == "/api/v1/services/aigc/image-generation/generation"
    assert KNOWN_MODELS["wan2.7-i2v"].cancel_path is None
    assert KNOWN_MODELS["fun-asr"].query_path == "/api/v1/tasks/{task_id}"


def test_require_model_fails_closed_for_missing_or_wrong_capability():
    visible = visible_models([{"id": "qwen-plus", "owned_by": "ali"}])
    with pytest.raises(ValueError, match="unavailable"):
        require_model("qwen-max", "chat", visible)
    with pytest.raises(ValueError, match="does not support"):
        require_model("qwen-plus", "image_generation", visible)
