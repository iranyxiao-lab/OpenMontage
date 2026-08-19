from __future__ import annotations

from tools.gateway_model_catalog import KNOWN_MODELS
from tools.gateway_model_smoke import GatewayModelSmoke, _provider_error_code, _task_id


def test_smoke_helpers_parse_task_envelopes_without_urls():
    body = {"output": {"task_id": "task-1", "task_status": "FAILED", "code": "ProviderError"}}
    assert _task_id(body) == "task-1"
    assert _provider_error_code(body) == "ProviderError"


def test_chat_smoke_uses_translation_options_for_qwen_mt():
    captured = {}

    class Client:
        def model_chat(self, **kwargs):
            captured.update(kwargs)
            return {"choices": []}

    item = KNOWN_MODELS["qwen-mt-turbo"]
    result = GatewayModelSmoke(Client())._chat(item)
    assert result.passed is True
    assert captured["translation_options"] == {
        "source_lang": "English",
        "target_lang": "Chinese",
    }


def test_chat_smoke_uses_multimodal_content_for_omni_models():
    captured = {}

    class Client:
        def model_chat(self, **kwargs):
            captured.update(kwargs)
            return {"choices": []}

    result = GatewayModelSmoke(Client())._chat(KNOWN_MODELS["qwen-omni-turbo"])
    assert result.passed is True
    assert captured["messages"][0]["content"][0]["type"] == "text"
