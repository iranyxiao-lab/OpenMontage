"""Real, low-cost smoke validation for the approved internal-gateway models.

The command emits only channel/model/capability/status/error-code metadata. It
never prints credentials, request bodies, provider result URLs, or signed OSS
URLs. Paid asynchronous submissions are never retried.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import time
import wave
from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any, Iterable

from PIL import Image

from openmontage.runner.worker import AlibabaOssObjectStoreClient
from tools.gateway_client import GatewayClient, GatewayRequestError, _task_status
from tools.gateway_model_catalog import GatewayModel, expected_model_count


TERMINAL_SUCCESS = {"succeeded", "success", "completed"}
TERMINAL_FAILURE = {"failed", "cancelled", "canceled", "error"}


@dataclass(frozen=True)
class SmokeResult:
    model: str
    channel: str
    capability: str
    passed: bool
    phase: str
    status: str
    error_code: str | None = None


class GatewayModelSmoke:
    def __init__(self, client: GatewayClient, *, poll_timeout: float = 600.0):
        self.client = client
        self.poll_timeout = poll_timeout
        self._image = _reference_image()
        self._image_data_uri = "data:image/png;base64," + base64.b64encode(self._image).decode("ascii")
        self._audio_url: str | None = None

    def validate(self, models: Iterable[GatewayModel]) -> list[SmokeResult]:
        return [self._validate_one(item) for item in models]

    def _validate_one(self, item: GatewayModel) -> SmokeResult:
        try:
            if item.capability == "chat":
                return self._chat(item)
            if item.capability == "embedding":
                self.client.model_embedding(model=item.model_id, input="openmontage smoke")
                return _passed(item, "request")
            if item.capability == "tts":
                body = self.client.model_speech(
                    model=item.model_id,
                    input="OpenMontage smoke test.",
                    voice="Cherry",
                    response_format="mp3",
                )
                if not body:
                    return _failed(item, "response", "empty_audio", "EMPTY_RESPONSE")
                return _passed(item, "response")
            if item.capability == "stt":
                return self._stt(item)
            if item.capability == "image_generation":
                return self._image_generation(item)
            if item.capability == "video_generation":
                return self._video_generation(item)
            return _failed(item, "catalog", "unsupported_capability", "UNSUPPORTED_CAPABILITY")
        except GatewayRequestError as exc:
            return _failed(
                item,
                "request",
                f"http_{exc.status_code}" if exc.status_code else "client_error",
                exc.code or "GATEWAY_REQUEST_FAILED",
            )
        except Exception as exc:  # validation must continue across the complete catalog
            return _failed(item, "client", "exception", type(exc).__name__)

    def _chat(self, item: GatewayModel) -> SmokeResult:
        kwargs: dict[str, Any] = {"max_tokens": 8, "stream": False}
        if item.model_id.startswith("qwen-mt-"):
            kwargs["translation_options"] = {
                "source_lang": "English",
                "target_lang": "Chinese",
            }
        if item.model_id == "qwen3-14b":
            kwargs["enable_thinking"] = False
        if item.model_id.startswith("qwen3.5-omni-plus"):
            kwargs["max_tokens"] = 10
        if item.model_id == "qvq-max":
            kwargs["stream"] = True
        self.client.model_chat(
            model=item.model_id,
            messages=[{"role": "user", "content": "Reply with OK."}],
            **kwargs,
        )
        return _passed(item, "response")

    def _image_generation(self, item: GatewayModel) -> SmokeResult:
        if item.protocol == "openai-multipart":
            body = self.client.model_image_edit(
                model=item.model_id,
                prompt="Keep the blue square unchanged.",
                image=self._image,
                n=1,
            )
        elif item.channel == "byteplus-modelark":
            body = self.client.model_image(
                model=item.model_id,
                prompt="A centered blue square on a white background.",
                size="1920x1920",
                n=1,
            )
        else:
            if item.model_id in {"wan2.7-image", "wan2.7-image-pro"}:
                body = self.client.model_image(
                    model=item.model_id,
                    prompt="A centered blue square on a white background.",
                    input={"prompt": "A centered blue square on a white background."},
                    size="1024*1024",
                )
            else:
                body = self.client.model_image(
                    model=item.model_id,
                    prompt="A centered blue square on a white background.",
                    n=1,
                )
        return self._complete_async(item, body)

    def _video_generation(self, item: GatewayModel) -> SmokeResult:
        prompt = "A blue square moving slowly on a white background."
        if item.protocol == "byteplus-task":
            payload = {
                "model": item.model_id,
                "content": [{"type": "text", "text": prompt}],
                "resolution": "720p",
                "ratio": "16:9",
                "duration": 4,
                "generate_audio": False,
                "watermark": False,
            }
        elif item.model_id == "wan2.7-t2v":
            payload = {
                "model": item.model_id,
                "input": {"prompt": prompt},
                "parameters": {"resolution": "720P", "duration": 2, "watermark": False},
            }
        elif item.model_id == "wan2.7-i2v":
            payload = {
                "model": item.model_id,
                "input": {
                    "prompt": prompt,
                    "media": [{"type": "first_frame", "url": self._image_data_uri}],
                },
                "parameters": {"resolution": "720P", "duration": 2, "watermark": False},
            }
        else:
            parameters: dict[str, Any] = {
                "resolution": "480P",
                "duration": 5,
                "watermark": False,
            }
            if item.model_id == "wan2.5-i2v-preview":
                parameters["audio"] = False
            payload = {
                "model": item.model_id,
                "input": {"prompt": prompt, "img_url": self._image_data_uri},
                "parameters": parameters,
            }
        body = self.client.submit_task(
            model=item.model_id,
            capability="video_generation",
            payload=payload,
        )
        return self._complete_async(item, body)

    def _stt(self, item: GatewayModel) -> SmokeResult:
        if self._audio_url is None:
            self._audio_url = _oss_reference_audio_url()
        body = self.client.model_transcription(
            model=item.model_id,
            file_url=self._audio_url,
            parameters={"language_hints": ["en"]},
        )
        return self._complete_async(item, body)

    def _complete_async(self, item: GatewayModel, body: dict[str, Any]) -> SmokeResult:
        task_id = _task_id(body)
        if not task_id:
            return _passed(item, "response")
        if not item.query_path:
            return _failed(item, "submit", "task_without_query", "TASK_QUERY_UNAVAILABLE")
        deadline = time.monotonic() + self.poll_timeout
        while time.monotonic() < deadline:
            current = self.client.task(
                model=item.model_id,
                capability=item.capability,
                task_id=task_id,
            )
            status = _task_status(current)
            if status in TERMINAL_SUCCESS:
                return _passed(item, "terminal", status)
            if status in TERMINAL_FAILURE:
                return _failed(item, "terminal", status, _provider_error_code(current))
            time.sleep(3.0)
        return _failed(item, "poll", "timeout", "POLL_TIMEOUT")


def _reference_image() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (512, 512), color=(20, 90, 220)).save(output, format="PNG")
    return output.getvalue()


def _reference_audio() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16_000)
        stream.writeframes(b"\0\0" * 16_000)
    return output.getvalue()


def _oss_reference_audio_url() -> str:
    bucket = os.environ.get("OPENMONTAGE_OSS_BUCKET", "").strip()
    region = os.environ.get("OPENMONTAGE_OSS_REGION", "").strip()
    endpoint = os.environ.get("OPENMONTAGE_OSS_ENDPOINT", "").strip() or None
    store = AlibabaOssObjectStoreClient(bucket, region, endpoint)
    key = os.environ.get("OPENMONTAGE_SMOKE_AUDIO_KEY", "").strip()
    if not key:
        key = "validation/gateway-model-smoke/reference-silence.wav"
        body = _reference_audio()
        import hashlib

        store.put(
            key,
            body,
            sha256="sha256:" + hashlib.sha256(body).hexdigest(),
            max_bytes=len(body),
        )
    signed = store._client.presign(  # noqa: SLF001 - validation-only signed read
        store._oss.GetObjectRequest(bucket=bucket, key=key),  # noqa: SLF001
        expires=timedelta(minutes=15),
    )
    return str(signed.url)


def _task_id(body: dict[str, Any]) -> str:
    output = body.get("output")
    output_id = output.get("task_id") if isinstance(output, dict) else None
    return str(body.get("id") or body.get("task_id") or output_id or "").strip()


def _provider_error_code(body: dict[str, Any]) -> str:
    output = body.get("output")
    output_code = output.get("code") if isinstance(output, dict) else None
    error = body.get("error")
    error_code = error.get("code") if isinstance(error, dict) else None
    return str(body.get("code") or output_code or error_code or "PROVIDER_TASK_FAILED")[:128]


def _passed(item: GatewayModel, phase: str, status: str = "ok") -> SmokeResult:
    return SmokeResult(item.model_id, item.channel, item.capability, True, phase, status)


def _failed(item: GatewayModel, phase: str, status: str, code: str) -> SmokeResult:
    return SmokeResult(item.model_id, item.channel, item.capability, False, phase, status, code)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", action="append", default=[])
    parser.add_argument("--poll-timeout", type=float, default=600.0)
    args = parser.parse_args()

    client = GatewayClient()
    catalog = client.catalog()
    selected = sorted(
        (catalog[model] for model in args.model if model in catalog)
        if args.model
        else catalog.values(),
        key=lambda item: (item.channel, item.capability, item.model_id),
    )
    if not args.model and len(selected) != expected_model_count():
        print(json.dumps({"error": "catalog_count_mismatch", "visible": len(selected)}))
        return 2
    results = GatewayModelSmoke(client, poll_timeout=args.poll_timeout).validate(selected)
    for result in results:
        print(json.dumps(asdict(result), separators=(",", ":"), sort_keys=True))
    passed = sum(result.passed for result in results)
    print(json.dumps({"summary": {"total": len(results), "passed": passed, "failed": len(results) - passed}}))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
