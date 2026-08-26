from __future__ import annotations

import requests
import pytest

from tools.gateway_client import (
    GatewayClient,
    GatewayConfigurationError,
    GatewayConfig,
    gateway_config,
    redact_error,
    route_for_model,
)


def test_gateway_config_is_opt_in_and_requires_both_values(monkeypatch):
    monkeypatch.delenv("OPENMONTAGE_GATEWAY_BASE_URL", raising=False)
    monkeypatch.delenv("OPENMONTAGE_GATEWAY_API_KEY", raising=False)
    assert gateway_config() is None

    monkeypatch.setenv("OPENMONTAGE_GATEWAY_BASE_URL", "http://gateway.example")
    with pytest.raises(GatewayConfigurationError):
        gateway_config()


def test_gateway_config_normalizes_url_and_group(monkeypatch):
    monkeypatch.setenv("OPENMONTAGE_GATEWAY_BASE_URL", "http://gateway.example/")
    monkeypatch.setenv("OPENMONTAGE_GATEWAY_API_KEY", "secret-key")
    monkeypatch.setenv("OPENMONTAGE_GATEWAY_GROUP", "sondo")
    assert gateway_config() == GatewayConfig("http://gateway.example", "secret-key", "sondo")


def test_openai_client_receives_explicit_gateway_endpoint(monkeypatch):
    monkeypatch.setenv("OPENMONTAGE_GATEWAY_BASE_URL", "http://gateway.example/")
    monkeypatch.setenv("OPENMONTAGE_GATEWAY_API_KEY", "secret-key")
    client = __import__("tools.gateway_client", fromlist=["openai_client"]).openai_client()
    assert str(client.base_url).rstrip("/") == "http://gateway.example/v1"


def test_route_registry_keeps_protocol_families_distinct():
    assert route_for_model("gpt-image-2", "image") == "/v1/images/generations"
    assert route_for_model("tts-1", "tts") == "/v1/audio/speech"
    assert route_for_model("whisper-1", "stt") == "/v1/audio/transcriptions"
    assert route_for_model("gemini-2.5-flash") == "native-gemini"
    assert route_for_model("mureka-1", "music") == "/mureka/*"


def test_gateway_request_retries_and_model_catalog_is_id_only(monkeypatch):
    config = GatewayConfig("http://gateway.example", "secret-key", "sondo")
    calls = []

    class Response:
        def __init__(self, status, body):
            self.status_code = status
            self._body = body

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(f"status {self.status_code}")

        def json(self):
            return self._body

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if len(calls) == 1:
            return Response(503, {})
        return Response(200, {"data": [{"id": "gpt-5-mini", "secret": "must-not-leak"}]})

    monkeypatch.setattr("tools.gateway_client.requests.request", request)
    monkeypatch.setattr("tools.gateway_client.time.sleep", lambda _: None)
    assert GatewayClient(config).models() == ["gpt-5-mini"]
    assert calls[0][2]["headers"]["Authorization"] == "Bearer secret-key"


def test_model_records_keep_only_non_sensitive_catalog_fields(monkeypatch):
    config = GatewayConfig("http://gateway.example", "secret-key", "sondo")

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [{
                    "id": "qwen-plus",
                    "owned_by": "ali",
                    "supported_endpoint_types": ["openai"],
                    "api_key": "must-not-leak",
                }]
            }

    monkeypatch.setattr("tools.gateway_client.requests.request", lambda *args, **kwargs: Response())
    assert GatewayClient(config).model_records() == [{
        "id": "qwen-plus",
        "owned_by": "ali",
        "supported_endpoint_types": ["openai"],
    }]


def test_task_submission_is_not_retried(monkeypatch):
    config = GatewayConfig("http://gateway.example", "secret-key", "sondo")
    calls = []

    class Response:
        status_code = 503

        def raise_for_status(self):
            raise requests.HTTPError("status 503")

        def json(self):
            return {}

    def request(*args, **kwargs):
        calls.append((args, kwargs))
        if args[0] == "GET":
            class Models:
                status_code = 200
                def raise_for_status(self):
                    return None
                def json(self):
                    return {"data": [{"id": "dreamina-seedance-2-0-fast-260128", "owned_by": "byteplus-modelark"}]}
            return Models()
        return Response()

    monkeypatch.setattr("tools.gateway_client.requests.request", request)
    with pytest.raises(Exception, match="HTTP 503"):
        GatewayClient(config).submit_task(
            model="dreamina-seedance-2-0-fast-260128",
            capability="video_generation",
            payload={"model": "dreamina-seedance-2-0-fast-260128"},
        )
    assert len(calls) == 2  # one catalog read and one non-retried submit


def test_bailian_video_submission_enables_async_protocol(monkeypatch):
    from tools.gateway_model_catalog import KNOWN_MODELS

    config = GatewayConfig("http://gateway.example", "secret-key", "sondo")
    calls = []

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"output": {"task_id": "task-1", "task_status": "PENDING"}}

    monkeypatch.setattr(
        "tools.gateway_client.GatewayClient.resolve_model",
        lambda self, model, capability: KNOWN_MODELS["wan2.7-t2v"],
    )
    monkeypatch.setattr(
        "tools.gateway_client.requests.request",
        lambda *args, **kwargs: calls.append(kwargs) or Response(),
    )

    GatewayClient(config).submit_task(
        model="wan2.7-t2v",
        capability="video_generation",
        payload={"model": "wan2.7-t2v", "input": {"prompt": "test"}},
    )
    assert calls[0]["headers"]["X-DashScope-Async"] == "enable"


def test_error_redaction_removes_key_and_urls(monkeypatch):
    monkeypatch.setenv("OPENMONTAGE_GATEWAY_API_KEY", "secret-key")
    safe = redact_error("secret-key https://gateway.example/v1?signature=abc details")
    assert "secret-key" not in safe
    assert "https://" not in safe


def test_task_helpers_ignore_non_object_nested_values():
    from tools.gateway_client import _result_url, _task_status

    assert _task_status({"output": "not-an-object"}) == ""
    assert _result_url({"output": ["not-an-object"], "content": "not-an-object"}) == ""
    assert _result_url({"output": {"results": [{"url": "https://media.example/image.png"}]}}) == "https://media.example/image.png"


def test_fun_asr_requires_an_https_object_url(monkeypatch):
    from tools.gateway_client import GatewayClient

    config = GatewayConfig("http://gateway.example", "secret-key", "sondo")
    monkeypatch.setattr(
        "tools.gateway_client.GatewayClient.resolve_model",
        lambda self, model, capability: __import__("tools.gateway_model_catalog", fromlist=["KNOWN_MODELS"]).KNOWN_MODELS["fun-asr"],
    )
    with pytest.raises(Exception, match="HTTPS file URL"):
        GatewayClient(config).model_transcription(model="fun-asr", file_url="http://example.test/a.wav")


def test_binary_speech_response_is_not_decoded_as_json(monkeypatch):
    from tools.gateway_model_catalog import KNOWN_MODELS

    config = GatewayConfig("http://gateway.example", "secret-key", "sondo")

    class Response:
        status_code = 200
        content = b"audio-bytes"

    monkeypatch.setattr("tools.gateway_client.GatewayClient.resolve_model", lambda *args: KNOWN_MODELS["qwen3-tts-flash"])
    monkeypatch.setattr("tools.gateway_client.requests.request", lambda *args, **kwargs: Response())
    assert GatewayClient(config).model_speech(
        model="qwen3-tts-flash", input="hello", voice="Cherry"
    ) == b"audio-bytes"


def test_openai_gateway_tts_model_is_registered():
    from tools.gateway_model_catalog import KNOWN_MODELS

    assert KNOWN_MODELS["tts-1"].capability == "tts"
    assert KNOWN_MODELS["tts-1"].submit_path == "/v1/audio/speech"


def test_structured_gateway_error_exposes_only_status_and_code(monkeypatch):
    config = GatewayConfig("http://gateway.example", "secret-key", "sondo")

    class Response:
        status_code = 403

        def json(self):
            return {"code": "AccessDenied", "message": "sensitive provider detail"}

    monkeypatch.setattr("tools.gateway_client.requests.request", lambda *args, **kwargs: Response())
    with pytest.raises(Exception) as caught:
        GatewayClient(config).request_json("GET", "/test")
    assert caught.value.status_code == 403
    assert caught.value.code == "AccessDenied"
    assert "sensitive provider detail" not in str(caught.value)


def test_streaming_chat_is_aggregated_without_logging_payload(monkeypatch):
    from tools.gateway_model_catalog import KNOWN_MODELS

    config = GatewayConfig("http://gateway.example", "secret-key", "sondo")

    class Response:
        status_code = 200
        text = 'data: {"choices":[{"delta":{"content":"OK"}}]}\n\ndata: [DONE]\n'

    monkeypatch.setattr("tools.gateway_client.GatewayClient.resolve_model", lambda *args: KNOWN_MODELS["qvq-max"])
    monkeypatch.setattr("tools.gateway_client.requests.post", lambda *args, **kwargs: Response())
    result = GatewayClient(config).model_chat(
        model="qvq-max", messages=[{"role": "user", "content": "x"}], stream=True
    )
    assert result["choices"][0]["message"]["content"] == "OK"
