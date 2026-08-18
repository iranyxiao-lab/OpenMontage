from __future__ import annotations

import hashlib
import importlib
import io
import json
import os
import re
import signal
import sys
import threading
import time
import uuid
import tempfile
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .contracts import Cancel, Command, Event, Grant, Ref


@dataclass(frozen=True)
class StageExecution:
    checkpoint: Ref
    artifacts: list[dict[str, Any]]


class ObjectStoreClient:
    """Minimal grant-scoped object-store boundary used by the runner.

    Production adapters implement PUT/HEAD with the task grant. The deterministic
    adapter below keeps tests hermetic and never exposes a URL or credential.
    """

    def __init__(self):
        self._objects: dict[str, bytes] = {}

    def put(self, object_key: str, body: bytes, *, sha256: str, max_bytes: int) -> Ref:
        if len(body) > max_bytes:
            raise ValueError("output_limit_exceeded")
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        if digest != sha256:
            raise ValueError("output_digest_mismatch")
        self._objects[object_key] = body
        return Ref(objectKey=object_key, sha256=digest, sizeBytes=len(body))

    def head(self, ref: Ref) -> bool:
        body = self._objects.get(ref.objectKey)
        return body is not None and len(body) == ref.sizeBytes and "sha256:" + hashlib.sha256(body).hexdigest() == ref.sha256

    def get(self, ref: Ref, *, max_bytes: int) -> bytes:
        body = self._objects.get(ref.objectKey)
        if body is None or len(body) > max_bytes:
            raise ValueError("input_not_available")
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        if len(body) != ref.sizeBytes or digest != ref.sha256:
            raise ValueError("input_digest_mismatch")
        return body


class AlibabaOssObjectStoreClient(ObjectStoreClient):
    """Grant-scoped Alibaba Cloud OSS adapter.

    The worker receives no OSS credential in a grant. Credentials are obtained
    from the pod workload identity and are used only with normalized object keys
    that have already passed GrantStore authorization.
    """

    def __init__(self, bucket: str, region: str, endpoint: str | None = None,
                 client: Any | None = None, sdk: Any | None = None):
        if not bucket or not region:
            raise ValueError("oss_configuration_missing")
        self.bucket = bucket
        if sdk is None:
            import alibabacloud_oss_v2 as oss
        else:
            oss = sdk

        if client is None:
            cfg = oss.config.load_default()
            cfg.region = region
            if endpoint:
                cfg.endpoint = endpoint
            cfg.credentials_provider = _oss_credentials_provider(oss)
            client = oss.Client(cfg)
        self._client = client
        self._oss = oss

    def put(self, object_key: str, body: bytes, *, sha256: str, max_bytes: int) -> Ref:
        if len(body) > max_bytes:
            raise ValueError("output_limit_exceeded")
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        if digest != sha256:
            raise ValueError("output_digest_mismatch")
        try:
            self._client.put_object(self._oss.PutObjectRequest(
                bucket=self.bucket,
                key=object_key,
                body=io.BytesIO(body),
                metadata={"sha256": digest},
                forbid_overwrite=False,
            ))
        except Exception as exc:
            raise RuntimeError("oss_put_failed") from exc
        return Ref(objectKey=object_key, sha256=digest, sizeBytes=len(body))

    def head(self, ref: Ref) -> bool:
        try:
            result = self._client.head_object(self._oss.HeadObjectRequest(bucket=self.bucket, key=ref.objectKey))
        except Exception as exc:
            raise RuntimeError("oss_head_failed") from exc
        metadata = getattr(result, "metadata", None) or {}
        stored_digest = metadata.get("sha256") or metadata.get("x-oss-meta-sha256")
        return int(getattr(result, "content_length", -1)) == ref.sizeBytes and stored_digest == ref.sha256

    def get(self, ref: Ref, *, max_bytes: int) -> bytes:
        try:
            result = self._client.get_object(self._oss.GetObjectRequest(bucket=self.bucket, key=ref.objectKey))
            body = result.body.read(max_bytes + 1) if hasattr(result.body, "read") else bytes(result.body)
        except Exception as exc:
            raise RuntimeError("oss_get_failed") from exc
        if len(body) > max_bytes:
            raise ValueError("input_limit_exceeded")
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        if len(body) != ref.sizeBytes or digest != ref.sha256:
            raise ValueError("input_digest_mismatch")
        return body


def _oss_credentials_provider(oss: Any) -> Any:
    """Load OSS_ACCESS_KEY_* and optional OSS_SESSION_TOKEN from the environment."""
    return oss.credentials.EnvironmentVariableCredentialsProvider()


class GrantStore:
    def __init__(self):
        self._grants: dict[tuple[str, int, int, str, str, str], Grant] = {}

    def add(self, grant: Grant) -> None:
        for target in grant.targets:
            self._grants[(grant.jobId, grant.attempt, grant.runRevision, grant.stage,
                          grant.method, target.objectKey)] = grant

    def authorize(self, command: Command, method: str, object_key: str, size: int) -> None:
        grant = self._grants.get((command.jobId, command.attempt, command.runRevision,
                                  command.stage, method, object_key))
        if grant is None or grant.expiresAt <= datetime.now(timezone.utc):
            raise PermissionError("task_grant_denied")
        target = next((item for item in grant.targets if item.objectKey == object_key), None)
        if target is None or size > target.maxBytes:
            raise PermissionError("task_grant_limit_exceeded")


class HeadlessAgent:
    def run(self, command: Command, workspace: Path) -> bytes:
        raise NotImplementedError


class DeterministicHeadlessAgent(HeadlessAgent):
    def run(self, command: Command, workspace: Path) -> bytes:
        return _json({"identity": f"{command.jobId}:{command.attempt}:{command.runRevision}:{command.stage}", "manifestVersion": command.manifestVersion, "stage": command.stage}).encode()


class PinnedManifestHeadlessAgent(HeadlessAgent):
    """Run only an entrypoint declared by an immutable, digest-pinned bundle."""

    _ENTRYPOINT = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")

    def __init__(self, manifest_root: str):
        self.root = Path(manifest_root)

    def run(self, command: Command, workspace: Path) -> bytes:
        digest = command.manifestVersion.removeprefix("sha256:")
        bundle = self.root / digest
        manifest_path = bundle / "manifest.json"
        if not manifest_path.is_file():
            raise RuntimeError("manifest_digest_not_available")
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        if manifest.get("manifestVersion") != command.manifestVersion:
            raise RuntimeError("manifest_version_mismatch")
        stage = manifest.get("stages", {}).get(command.stage)
        entrypoint = stage.get("entrypoint") if isinstance(stage, dict) else None
        if not isinstance(entrypoint, str) or not self._ENTRYPOINT.fullmatch(entrypoint):
            raise RuntimeError("manifest_entrypoint_invalid")
        module_name, function_name = entrypoint.split(":", 1)
        sys.path.insert(0, str(bundle))
        try:
            sys.modules.pop(module_name, None)
            handler: Callable[..., Any] = getattr(importlib.import_module(module_name), function_name)
            payload = handler(command=command, workspace=workspace)
        finally:
            sys.path.pop(0)
        if not isinstance(payload, (bytes, bytearray)):
            raise RuntimeError("headless_agent_output_invalid")
        return bytes(payload)


class StageExecutor:
    def __init__(self, config: "RunnerConfig", object_store: ObjectStoreClient | None = None, grants: GrantStore | None = None, agent: HeadlessAgent | None = None):
        self.config = config
        self.object_store = object_store or self._build_object_store(config)
        self.grants = grants or GrantStore()
        self.agent = agent or (PinnedManifestHeadlessAgent(config.manifest_root) if config.manifest_root else DeterministicHeadlessAgent())

    def execute(self, command: Command) -> StageExecution:
        self._validate_pinned_bundle(command)
        identity = f"{command.jobId}:{command.attempt}:{command.runRevision}:{command.stage}"
        workspace = Path(self.config.workspace_root) / command.jobId / f"revision-{command.runRevision}" / command.stage / f"attempt-{command.attempt}"
        workspace.mkdir(parents=True, exist_ok=True)
        if self.config.require_grants:
            self.grants.authorize(command, "GET", command.runSpecRef.objectKey, command.runSpecRef.sizeBytes)
            run_spec = self.object_store.get(command.runSpecRef, max_bytes=command.runSpecRef.sizeBytes)
            (workspace / "run-spec.json").write_bytes(run_spec)
        payload = self.agent.run(command, workspace)
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        key = f"montage/{command.taskId}/run-{command.runRevision}/checkpoints/{command.stage}.json"
        if self.config.require_grants:
            self.grants.authorize(command, "PUT", key, len(payload))
        checkpoint = self.object_store.put(key, payload, sha256=digest, max_bytes=command.limits.maxOutputBytes)
        if self.config.require_grants:
            self.grants.authorize(command, "HEAD", key, checkpoint.sizeBytes)
        if not self.object_store.head(checkpoint):
            raise RuntimeError("checkpoint_receipt_invalid")
        return StageExecution(checkpoint=checkpoint, artifacts=[])

    @staticmethod
    def _build_object_store(config: "RunnerConfig") -> ObjectStoreClient:
        if config.object_store == "oss":
            return AlibabaOssObjectStoreClient(config.oss_bucket, config.oss_region, config.oss_endpoint)
        if config.object_store != "memory":
            raise ValueError("object_store_unsupported")
        return ObjectStoreClient()

    def _validate_pinned_bundle(self, command: Command) -> None:
        if self.config.manifest_root:
            manifest = (Path(self.config.manifest_root)
                        / command.manifestVersion.removeprefix("sha256:") / "manifest.json")
            if not manifest.is_file():
                raise RuntimeError("manifest_digest_not_available")

    def cleanup(self, command: Command) -> None:
        root = Path(self.config.workspace_root) / command.jobId / f"revision-{command.runRevision}" / command.stage / f"attempt-{command.attempt}"
        if root.exists():
            for path in sorted(root.rglob("*"), reverse=True):
                if path.is_file(): path.unlink(missing_ok=True)
            root.rmdir()


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=lambda x: x.isoformat())


@dataclass
class RunnerConfig:
    redis_url: str = os.getenv("OPENMONTAGE_REDIS_URL", "redis://127.0.0.1:6379/0")
    commands_prefix: str = os.getenv("OPENMONTAGE_COMMANDS_PREFIX", "mediaworker.openmontage.commands.v1")
    events_stream: str = os.getenv("OPENMONTAGE_EVENTS_STREAM", "mediaworker.openmontage.events.v1")
    cancels_stream: str = os.getenv("OPENMONTAGE_CANCELS_STREAM", "mediaworker.openmontage.cancel.v1")
    grants_stream: str = os.getenv("OPENMONTAGE_GRANTS_STREAM", "mediaworker.openmontage.grant.v1")
    channel: str = os.getenv("OPENMONTAGE_CHANNEL", "stable")
    pool: str = os.getenv("OPENMONTAGE_POOL", "openmontage-render")
    namespace: str = os.getenv("OPENMONTAGE_NAMESPACE", "openmontage-test")
    pod_uid: str = os.getenv("POD_UID", "local")
    consumer_group: str = os.getenv("OPENMONTAGE_CONSUMER_GROUP", "")
    consumer_name: str = os.getenv("POD_NAME", "runner")
    workspace_root: str = os.getenv("OPENMONTAGE_WORKSPACE_ROOT", tempfile.gettempdir() + "/openmontage-attempts")
    drain_timeout_seconds: int = int(os.getenv("OPENMONTAGE_DRAIN_TIMEOUT_SECONDS", "30"))
    require_grants: bool = os.getenv("OPENMONTAGE_REQUIRE_GRANTS", "false").lower() == "true"
    object_store: str = os.getenv("OPENMONTAGE_OBJECT_STORE", "memory")
    oss_bucket: str = os.getenv("OPENMONTAGE_OSS_BUCKET", "")
    oss_region: str = os.getenv("OPENMONTAGE_OSS_REGION", "")
    oss_endpoint: str = os.getenv("OPENMONTAGE_OSS_ENDPOINT", "") or None
    manifest_root: str = os.getenv("OPENMONTAGE_MANIFEST_ROOT", "")

    @property
    def command_stream(self) -> str:
        return f"{self.commands_prefix}.{self.channel}.{self.pool}"

    @property
    def worker_id(self) -> str:
        return f"{self.pool}/{self.namespace}/{self.pod_uid}"

    @property
    def group(self) -> str:
        return self.consumer_group or f"openmontage-v1-{self.channel}-{self.pool}"


class RedisTransport:
    def __init__(self, config: RunnerConfig):
        import redis

        self.config = config
        self.client = redis.Redis.from_url(config.redis_url, decode_responses=True)
        for stream, group in ((config.command_stream, config.group),
                              (config.cancels_stream, config.group + "-cancel"),
                              (config.grants_stream, config.group + "-grant")):
            try:
                self.client.xgroup_create(stream, group, id="0-0", mkstream=True)
            except redis.ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise

    def read(self, timeout_ms: int = 1000) -> list[tuple[str, dict[str, str]]]:
        claimed = self.client.xautoclaim(self.config.command_stream, self.config.group,
                                         self.config.consumer_name, min_idle_time=120_000,
                                         start_id="0-0", count=1)
        if len(claimed) > 1 and claimed[1]:
            return list(claimed[1])
        result = self.client.xreadgroup(self.config.group, self.config.consumer_name,
                                        {self.config.command_stream: ">"}, count=1, block=timeout_ms)
        if not result:
            return []
        return [(message_id, fields) for _, entries in result for message_id, fields in entries]

    def ack(self, message_id: str) -> None:
        self.client.xack(self.config.command_stream, self.config.group, message_id)

    def read_cancels(self, timeout_ms: int = 1) -> list[tuple[str, dict[str, str]]]:
        result = self.client.xreadgroup(self.config.group + "-cancel", self.config.consumer_name,
                                        {self.config.cancels_stream: ">"}, count=20, block=timeout_ms)
        if not result:
            return []
        return [(message_id, fields) for _, entries in result for message_id, fields in entries]

    def ack_cancel(self, message_id: str) -> None:
        self.client.xack(self.config.cancels_stream, self.config.group + "-cancel", message_id)

    def read_grants(self, timeout_ms: int = 1) -> list[tuple[str, dict[str, str]]]:
        result = self.client.xreadgroup(self.config.group + "-grant", self.config.consumer_name,
                                        {self.config.grants_stream: ">"}, count=20, block=timeout_ms)
        if not result:
            return []
        return [(message_id, fields) for _, entries in result for message_id, fields in entries]

    def ack_grant(self, message_id: str) -> None:
        self.client.xack(self.config.grants_stream, self.config.group + "-grant", message_id)

    def publish(self, event: Event) -> str:
        return self.client.xadd(self.config.events_stream, {"payload": _json(event.model_dump(mode="json"))})

    def publish_dlq(self, summary: str) -> str:
        return self.client.xadd(self.config.events_stream.replace("events.v1", "dlq.v1"),
                                {"errorCode": "INVALID_SCHEMA", "summary": summary[:1024]})


class Runner:
    def __init__(self, config: RunnerConfig | None = None, transport: RedisTransport | None = None, executor: StageExecutor | None = None):
        self.config = config or RunnerConfig()
        self.transport = transport
        self.executor = executor or StageExecutor(self.config)
        self.draining = False
        self._stop = threading.Event()
        self._completed: set[str] = set()
        self._cancelled: set[str] = set()

    def identity(self, command: Command) -> str:
        return f"{command.jobId}:{command.attempt}:{command.runRevision}:{command.stage}"

    def handle(self, command: Command) -> Event:
        identity = self.identity(command)
        cancel_identity = f"{command.jobId}:{command.attempt}:{command.runRevision}"
        if cancel_identity in self._cancelled:
            raise RuntimeError("attempt_cancelled")
        if identity in self._completed:
            return self._success(command, deterministic=True)
        if self.draining:
            raise RuntimeError("runner_draining")
        execution = self.executor.execute(command)
        self._completed.add(identity)
        self.executor.cleanup(command)
        return self._success(command, checkpoint=execution.checkpoint, artifacts=execution.artifacts)

    def _success(self, command: Command, checkpoint: Ref | None = None, artifacts: list[dict[str, Any]] | None = None, deterministic: bool = False) -> Event:
        now = datetime.now(timezone.utc)
        return Event(schemaVersion="openmontage.event.v1", eventId=f"event-{uuid.uuid4().hex}",
                     jobId=command.jobId, taskId=command.taskId, runId=command.runId,
                     attempt=command.attempt, runRevision=command.runRevision, type="StageSucceeded",
                     occurredAt=now, workerId=self.config.worker_id, workerPool=self.config.pool,
                     channel=self.config.channel, stage=command.stage, checkpointRef=checkpoint, artifacts=artifacts or None)

    def loop(self) -> None:
        if self.transport is None:
            self.transport = RedisTransport(self.config)
        while not self._stop.is_set():
            for message_id, fields in self.transport.read_cancels(timeout_ms=1):
                try:
                    cancel = Cancel.model_validate_json(fields["payload"])
                    self._cancelled.add(f"{cancel.jobId}:{cancel.attempt}:{cancel.runRevision}")
                finally:
                    self.transport.ack_cancel(message_id)
            for message_id, fields in self.transport.read_grants(timeout_ms=1):
                try:
                    self.executor.grants.add(Grant.model_validate_json(fields["payload"]))
                finally:
                    self.transport.ack_grant(message_id)
            for message_id, fields in self.transport.read():
                try:
                    command = Command.model_validate_json(fields["payload"])
                    event = self.handle(command)
                    self.transport.publish(event)
                    self.transport.ack(message_id)
                except (ValidationError, ValueError, RuntimeError) as exc:
                    # Invalid or cancelled deliveries converge into the isolated DLQ before ACK.
                    self.transport.publish_dlq(type(exc).__name__ + ": " + str(exc))
                    self.transport.ack(message_id)

    def drain(self) -> None:
        self.draining = True
        self._stop.set()


def create_app(runner: Runner | None = None) -> FastAPI:
    active = runner or Runner()
    app = FastAPI(title="OpenMontage Runner", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    def health() -> dict[str, Any]:
        return {"ok": True, "workerId": active.config.worker_id, "draining": active.draining}

    @app.get("/readyz")
    def ready() -> JSONResponse:
        return JSONResponse({"ok": not active.draining}, status_code=200 if not active.draining else 503)

    @app.get("/metrics")
    def metrics() -> str:
        return f"openmontage_runner_draining {int(active.draining)}\nopenmontage_runner_completed {len(active._completed)}\n"

    return app


def main() -> None:
    import uvicorn

    runner = Runner()
    signal.signal(signal.SIGTERM, lambda *_: runner.drain())
    signal.signal(signal.SIGINT, lambda *_: runner.drain())
    # The API process remains probeable while the worker loop runs in a daemon thread.
    threading.Thread(target=runner.loop, name="openmontage-runner", daemon=True).start()
    uvicorn.run(create_app(runner), host="0.0.0.0", port=int(os.getenv("OPENMONTAGE_RUNNER_PORT", "4751")), log_level="info")


if __name__ == "__main__":
    main()
