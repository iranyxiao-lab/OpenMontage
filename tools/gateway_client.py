"""Explicit client for the internal AI gateway.

The gateway is opt-in so local development keeps the existing provider
behavior. When both gateway variables are present, callers must use this
client and must not silently fall back to a vendor endpoint.
"""

from __future__ import annotations

import os
import json
import time
from dataclasses import dataclass
from typing import Any

import requests

from tools.gateway_model_catalog import GatewayModel, model_catalog, visible_models


class GatewayConfigurationError(RuntimeError):
    """Raised when gateway configuration is incomplete or invalid."""


class GatewayRequestError(RuntimeError):
    """Raised for a gateway request after bounded retries."""

    def __init__(self, message: str, *, status_code: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


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
        retryable: bool = True,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.config.base_url}/{path.lstrip('/')}"
        headers = {**self._headers, **(extra_headers or {})}
        last_error: BaseException | None = None
        attempts = 3 if retryable else 1
        for attempt in range(attempts):
            try:
                response = requests.request(
                    method,
                    url,
                    headers=headers,
                    json=payload,
                    timeout=timeout,
                )
                self._raise_for_status(response)
                body = response.json()
                if not isinstance(body, dict):
                    raise GatewayRequestError("Gateway response was not an object")
                return body
            except (requests.RequestException, ValueError, GatewayRequestError) as exc:
                last_error = exc
                if attempt == attempts - 1 or _is_permanent_request_error(exc):
                    break
                time.sleep(0.25 * (2**attempt))
        if isinstance(last_error, GatewayRequestError):
            raise last_error
        raise GatewayRequestError(redact_error(last_error or "gateway request failed"))

    def request_bytes(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: float = 120.0,
        retryable: bool = True,
    ) -> bytes:
        """Return a binary provider response without attempting JSON decoding."""

        url = f"{self.config.base_url}/{path.lstrip('/')}"
        last_error: BaseException | None = None
        attempts = 3 if retryable else 1
        for attempt in range(attempts):
            try:
                response = requests.request(
                    method,
                    url,
                    headers=self._headers,
                    json=payload,
                    timeout=timeout,
                )
                self._raise_for_status(response)
                return response.content
            except (requests.RequestException, GatewayRequestError) as exc:
                last_error = exc
                if attempt == attempts - 1 or _is_permanent_request_error(exc):
                    break
                time.sleep(0.25 * (2**attempt))
        if isinstance(last_error, GatewayRequestError):
            raise last_error
        raise GatewayRequestError(redact_error(last_error or "gateway request failed"))

    def _raise_for_status(self, response: requests.Response) -> None:
        if response.status_code < 400:
            return
        code = None
        try:
            body = response.json()
            if isinstance(body, dict):
                error = body.get("error")
                code = body.get("code") or (error.get("code") if isinstance(error, dict) else None)
        except ValueError:
            pass
        safe_code = str(code).strip()[:128] if code else None
        raise GatewayRequestError(
            f"gateway request failed: HTTP {response.status_code} code={safe_code or 'unknown'}",
            status_code=response.status_code,
            code=safe_code,
        )

    def models(self) -> list[str]:
        return [item["id"] for item in self.model_records()]

    def model_records(self) -> list[dict[str, Any]]:
        """Return the non-sensitive model records advertised by the gateway."""

        body = self.request_json("GET", "/v1/models", timeout=30.0)
        data = body.get("data", [])
        if not isinstance(data, list):
            return []
        return [
            {
                key: item[key]
                for key in ("id", "object", "created", "owned_by", "supported_endpoint_types")
                if key in item
            }
            for item in data
            if isinstance(item, dict) and item.get("id")
        ]

    def catalog(self) -> dict[str, GatewayModel]:
        """Return registered models that are visible and owned by the expected channel."""

        return visible_models(self.model_records())

    def resolve_model(self, model: str, capability: str) -> GatewayModel:
        item = self.catalog().get(model)
        if item is None:
            raise GatewayRequestError(f"gateway model is unavailable: {model}")
        if item.status != "READY":
            reason = item.reason_code or "gateway_model_not_ready"
            raise GatewayRequestError(
                f"gateway model is not ready: {model} ({reason})",
                code=reason,
            )
        if item.capability != capability:
            raise GatewayRequestError(f"gateway model {model} does not support {capability}")
        return item

    def model_request(
        self,
        *,
        model: str,
        capability: str,
        payload: dict[str, Any],
        timeout: float = 120.0,
        retryable: bool = True,
    ) -> dict[str, Any]:
        item = self.resolve_model(model, capability)
        return self.request_json(
            "POST", item.submit_path, payload=payload, timeout=timeout, retryable=retryable
        )

    def chat(self, *, model: str, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        return self.request_json("POST", "/v1/chat/completions", payload={"model": model, "messages": messages, **kwargs})

    def model_chat(self, *, model: str, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("stream"):
            return self._stream_chat(model=model, messages=messages, kwargs=kwargs)
        return self.model_request(
            model=model,
            capability="chat",
            payload={"messages": messages, **kwargs, "model": model},
        )

    def _stream_chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        item = self.resolve_model(model, "chat")
        payload = {"messages": messages, **kwargs, "model": model}
        response = requests.post(
            f"{self.config.base_url}/{item.submit_path.lstrip('/')}",
            headers=self._headers,
            json=payload,
            timeout=180.0,
        )
        self._raise_for_status(response)
        content: list[str] = []
        final: dict[str, Any] = {"choices": [{"index": 0, "message": {"role": "assistant", "content": ""}}]}
        for line in response.text.splitlines():
            if not line.startswith("data:"):
                continue
            value = line[5:].strip()
            if value == "[DONE]":
                break
            try:
                chunk = json.loads(value)
            except ValueError:
                continue
            if not isinstance(chunk, dict):
                continue
            choices = chunk.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                delta = choices[0].get("delta")
                if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                    content.append(delta["content"])
                final["id"] = chunk.get("id")
                final["model"] = chunk.get("model", model)
        final["choices"][0]["message"]["content"] = "".join(content)
        return final

    def image(self, *, model: str, prompt: str, **kwargs: Any) -> dict[str, Any]:
        return self.request_json("POST", "/v1/images/generations", payload={"model": model, "prompt": prompt, **kwargs})

    def model_image(self, *, model: str, prompt: str, **kwargs: Any) -> dict[str, Any]:
        item = self.resolve_model(model, "image_generation")
        if item.protocol == "openai-multipart":
            raise GatewayRequestError("Use model_image_edit for multipart image-edit models")
        if item.protocol == "bailian-native":
            native_input = kwargs.pop("input", None)
            if not isinstance(native_input, dict):
                native_input = {"prompt": prompt}
            elif "prompt" not in native_input and prompt:
                native_input = {**native_input, "prompt": prompt}
            payload = {"input": native_input, "parameters": kwargs, "model": model}
            return self.request_json("POST", item.submit_path, payload=payload, retryable=False)
        if item.protocol == "bailian-task":
            payload = {"model": model, "input": {"prompt": prompt}, "parameters": kwargs}
            return self.submit_task(model=model, capability="image_generation", payload=payload)
        return self.model_request(
            model=model,
            capability="image_generation",
            payload={"prompt": prompt, **kwargs, "model": model},
            retryable=False,
        )

    def model_transcription(self, *, model: str, file_url: str, **kwargs: Any) -> dict[str, Any]:
        """Submit Fun-ASR using its native public-URL contract.

        The gateway intentionally does not accept local file paths for this
        provider. Callers must first persist the audio to an approved object
        store and pass the resulting HTTPS URL.
        """

        item = self.resolve_model(model, "stt")
        if item.protocol != "bailian-task":
            raise GatewayRequestError(f"gateway model {model} does not support native transcription")
        file_url = file_url.strip()
        if not file_url.startswith("https://"):
            raise GatewayRequestError("transcription requires an HTTPS file URL")
        payload = {
            "input": {"file_urls": [file_url]},
            **kwargs,
            "model": model,
        }
        return self.submit_task(model=model, capability="stt", payload=payload)

    def speech(self, *, model: str, input: str, voice: str, **kwargs: Any) -> dict[str, Any]:
        return self.request_json("POST", "/v1/audio/speech", payload={"model": model, "input": input, "voice": voice, **kwargs})

    def model_image_edit(
        self,
        *,
        model: str,
        prompt: str,
        image: bytes,
        image_name: str = "input.png",
        **kwargs: Any,
    ) -> dict[str, Any]:
        item = self.resolve_model(model, "image_generation")
        if item.protocol != "openai-multipart":
            raise GatewayRequestError(f"gateway model {model} is not an image-edit model")
        headers = {key: value for key, value in self._headers.items() if key != "Content-Type"}
        response = requests.post(
            f"{self.config.base_url}/{item.submit_path.lstrip('/')}",
            headers=headers,
            data={"model": model, "prompt": prompt, **kwargs},
            files={"image": (image_name, image, "image/png")},
            timeout=180.0,
        )
        self._raise_for_status(response)
        body = response.json()
        if not isinstance(body, dict):
            raise GatewayRequestError("Gateway response was not an object")
        return body

    def model_speech(self, *, model: str, input: str, voice: str, **kwargs: Any) -> bytes:
        item = self.resolve_model(model, "tts")
        return self.request_bytes(
            "POST",
            item.submit_path,
            payload={"input": input, "voice": voice, **kwargs, "model": model},
        )

    def native_model(self, *, model: str, capability: str, payload: dict[str, Any], timeout: float = 120.0) -> dict[str, Any]:
        """Send a provider-native JSON request using the registered route."""

        return self.submit_task(model=model, capability=capability, payload=payload, timeout=timeout)

    def transcription(self, *, model: str, file: Any, **kwargs: Any) -> dict[str, Any]:
        """Transcription is multipart at the gateway; callers can use the SDK for this route."""

        raise GatewayRequestError("Use the OpenAI SDK audio transcription client for multipart uploads")

    def embedding(self, *, model: str, input: str | list[str], **kwargs: Any) -> dict[str, Any]:
        return self.request_json("POST", "/v1/embeddings", payload={"model": model, "input": input, **kwargs})

    def model_embedding(self, *, model: str, input: str | list[str], **kwargs: Any) -> dict[str, Any]:
        return self.model_request(
            model=model,
            capability="embedding",
            payload={"input": input, **kwargs, "model": model},
        )

    def submit_task(self, *, model: str, capability: str, payload: dict[str, Any], timeout: float = 120.0) -> dict[str, Any]:
        """Submit a paid async task without retrying an ambiguous write."""
        item = self.resolve_model(model, capability)
        extra_headers = {"X-DashScope-Async": "enable"} if item.protocol == "bailian-task" else None
        return self.request_json(
            "POST", item.submit_path, payload=payload, timeout=timeout,
            retryable=False, extra_headers=extra_headers,
        )

    def task(self, *, model: str, capability: str, task_id: str, timeout: float = 60.0) -> dict[str, Any]:
        item = self.resolve_model(model, capability)
        if not item.query_path:
            raise GatewayRequestError(f"gateway model has no query route: {model}")
        return self.request_json(
            "GET", item.query_path.replace("{task_id}", task_id), timeout=timeout
        )

    def cancel_task(self, *, model: str, capability: str, task_id: str, timeout: float = 60.0) -> dict[str, Any]:
        item = self.resolve_model(model, capability)
        if not item.cancel_path:
            raise GatewayRequestError(f"gateway model has no cancel route: {model}")
        return self.request_json(
            item.cancel_method,
            item.cancel_path.replace("{task_id}", task_id),
            timeout=timeout,
            retryable=False,
        )

    def poll_task_until_terminal(
        self,
        *,
        model: str,
        capability: str,
        task_id: str,
        timeout: float = 900.0,
        interval: float = 2.0,
    ) -> dict[str, Any]:
        """Poll a registered task without logging provider response payloads."""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            body = self.task(model=model, capability=capability, task_id=task_id)
            status = _task_status(body)
            if status in {"succeeded", "success", "completed", "failed", "cancelled", "canceled", "error"}:
                return body
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
        raise GatewayRequestError("gateway task polling timed out")

    def download_task_result(self, body: dict[str, Any], output_path: str) -> str:
        """Download a terminal task URL without persisting the signed URL."""

        url = _result_url(body)
        if not url:
            raise GatewayRequestError("gateway task returned no media URL")
        response = requests.get(url, timeout=180.0, allow_redirects=False)
        response.raise_for_status()
        with open(output_path, "wb") as stream:
            stream.write(response.content)
        return output_path

    def native(self, path: str, payload: dict[str, Any], *, timeout: float = 120.0) -> dict[str, Any]:
        """Call a gateway-native route for protocols outside OpenAI SDK."""

        return self.request_json("POST", path, payload=payload, timeout=timeout)

    def poll(self, path: str, *, timeout: float = 60.0) -> dict[str, Any]:
        """Read an async task state without exposing response contents to logs."""

        return self.request_json("GET", path, timeout=timeout)


def _task_status(body: dict[str, Any]) -> str:
    output = body.get("output")
    output_status = output.get("task_status") if isinstance(output, dict) else None
    value = body.get("status") or body.get("task_status") or output_status
    return str(value or "").strip().lower()


def _is_permanent_request_error(error: BaseException) -> bool:
    return (
        isinstance(error, GatewayRequestError)
        and error.status_code is not None
        and error.status_code not in {408, 429}
        and error.status_code < 500
    )


def _result_url(body: dict[str, Any]) -> str:
    content = body.get("content")
    output = body.get("output")
    content_url = content.get("video_url") if isinstance(content, dict) else None
    output_video_url = output.get("video_url") if isinstance(output, dict) else None
    output_url = output.get("url") if isinstance(output, dict) else None
    output_results = output.get("results") if isinstance(output, dict) else None
    first_result = output_results[0] if isinstance(output_results, list) and output_results else None
    first_result_url = first_result.get("url") if isinstance(first_result, dict) else None
    first_result_video_url = first_result.get("video_url") if isinstance(first_result, dict) else None
    candidates = (
        body.get("video_url"),
        content_url,
        output_video_url,
        output_url,
        first_result_video_url,
        first_result_url,
    )
    return next((str(value).strip() for value in candidates if value), "")
