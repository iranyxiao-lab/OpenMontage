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


def test_error_redaction_removes_key_and_urls(monkeypatch):
    monkeypatch.setenv("OPENMONTAGE_GATEWAY_API_KEY", "secret-key")
    safe = redact_error("secret-key https://gateway.example/v1?signature=abc details")
    assert "secret-key" not in safe
    assert "https://" not in safe
