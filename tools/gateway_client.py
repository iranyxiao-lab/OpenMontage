"""Explicit client for the internal AI gateway.

The gateway is opt-in so local development keeps the existing provider
behavior. When both gateway variables are present, callers must use this
client and must not silently fall back to a vendor endpoint.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import requests


class GatewayConfigurationError(RuntimeError):
    """Raised when gateway configuration is incomplete or invalid."""


class GatewayRequestError(RuntimeError):
    """Raised for a gateway request after bounded retries."""


@dataclass(frozen=True)
class GatewayConfig:
    base_url: str
    api_key: str
    group: str = "sondo"

    @classmethod
    def from_env(cls) -> "GatewayConfig | None":
        base_url = os.environ.get("OPENMONTAGE_GATEWAY_BASE_URL", "").strip()
        api_key = os.environ.get("OPENMONTAGE_GATEWAY_API_KEY", "").strip()
        group = os.environ.get("OPENMONTAGE_GATEWAY_GROUP", "sondo").strip() or "sondo"
        if not base_url and not api_key:
            return None
        if not base_url or not api_key:
            raise GatewayConfigurationError(
                "OPENMONTAGE_GATEWAY_BASE_URL and OPENMONTAGE_GATEWAY_API_KEY must be set together"
            )
        if not base_url.startswith(("http://", "https://")):
            raise GatewayConfigurationError("OPENMONTAGE_GATEWAY_BASE_URL must use http or https")
        return cls(base_url=base_url.rstrip("/"), api_key=api_key, group=group)


def gateway_config() -> GatewayConfig | None:
    """Read gateway configuration without caching secrets between calls."""

    return GatewayConfig.from_env()


def gateway_configured() -> bool:
    """Return whether a complete gateway configuration is active."""

    return gateway_config() is not None


def redact_error(error: BaseException | str) -> str:
    """Return a bounded error string with credentials and query strings removed."""

    message = str(error)
    key = os.environ.get("OPENMONTAGE_GATEWAY_API_KEY", "").strip()
    if key:
        message = message.replace(key, "[redacted]")
    # Do not persist signed URLs or oversized provider payloads.
    import re

    message = re.sub(r"https?://[^\s]+", "[url-redacted]", message)
    return message[:500]


def openai_client() -> Any:
    """Build an OpenAI SDK client with gateway credentials when configured."""

    from openai import OpenAI

    config = gateway_config()
    if config is None:
        return OpenAI()
    return OpenAI(
        api_key=config.api_key,
        base_url=f"{config.base_url}/v1",
        timeout=120.0,
        max_retries=0,
        default_headers={"X-Token-Group": config.group},
    )


def route_for_model(model: str, capability: str = "chat") -> str:
    """Return the gateway route family used by an OpenMontage capability."""

    value = model.strip().lower()
    if value.startswith("gemini"):
        return "native-gemini"
    if capability == "image":
        return "/v1/images/generations"
    if capability == "tts":
        return "/v1/audio/speech"
    if capability == "stt":
        return "/v1/audio/transcriptions"
    if capability == "embedding":
        return "/v1/embeddings"
    if capability == "video":
        return "/v1/videos"
    if capability == "music":
        return "/mureka/*"
    return "/v1/chat/completions"


class GatewayClient:
    """Small requests-based client for model discovery and native routes."""

    def __init__(self, config: GatewayConfig | None = None):
        self.config = config or gateway_config()
        if self.config is None:
            raise GatewayConfigurationError("OpenMontage gateway is not configured")

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "X-Token-Group": self.config.group,
            "Content-Type": "application/json",
        }

    def request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        url = f"{self.config.base_url}/{path.lstrip('/')}"
        last_error: BaseException | None = None
        for attempt in range(3):
            try:
                response = requests.request(
                    method,
                    url,
                    headers=self._headers,
                    json=payload,
                    timeout=timeout,
                )
                if response.status_code in {408, 429} or response.status_code >= 500:
                    response.raise_for_status()
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict):
                    raise GatewayRequestError("Gateway response was not an object")
                return body
            except (requests.RequestException, ValueError, GatewayRequestError) as exc:
                last_error = exc
                if attempt == 2:
                    break
                time.sleep(0.25 * (2**attempt))
        raise GatewayRequestError(redact_error(last_error or "gateway request failed"))

    def models(self) -> list[str]:
        body = self.request_json("GET", "/v1/models", timeout=30.0)
        data = body.get("data", [])
        if not isinstance(data, list):
            return []
        return [str(item.get("id")) for item in data if isinstance(item, dict) and item.get("id")]

    def chat(self, *, model: str, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        return self.request_json("POST", "/v1/chat/completions", payload={"model": model, "messages": messages, **kwargs})

    def image(self, *, model: str, prompt: str, **kwargs: Any) -> dict[str, Any]:
        return self.request_json("POST", "/v1/images/generations", payload={"model": model, "prompt": prompt, **kwargs})

    def speech(self, *, model: str, input: str, voice: str, **kwargs: Any) -> dict[str, Any]:
        return self.request_json("POST", "/v1/audio/speech", payload={"model": model, "input": input, "voice": voice, **kwargs})

    def transcription(self, *, model: str, file: Any, **kwargs: Any) -> dict[str, Any]:
        """Transcription is multipart at the gateway; callers can use the SDK for this route."""

        raise GatewayRequestError("Use the OpenAI SDK audio transcription client for multipart uploads")

    def embedding(self, *, model: str, input: str | list[str], **kwargs: Any) -> dict[str, Any]:
        return self.request_json("POST", "/v1/embeddings", payload={"model": model, "input": input, **kwargs})

    def native(self, path: str, payload: dict[str, Any], *, timeout: float = 120.0) -> dict[str, Any]:
        """Call a gateway-native route for protocols outside OpenAI SDK."""

        return self.request_json("POST", path, payload=payload, timeout=timeout)

    def poll(self, path: str, *, timeout: float = 60.0) -> dict[str, Any]:
        """Read an async task state without exposing response contents to logs."""

        return self.request_json("GET", path, timeout=timeout)
