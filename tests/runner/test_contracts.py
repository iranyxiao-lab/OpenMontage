import json
import sys
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from openmontage.runner.contracts import Cancel, Command, Event, Grant, UserIntent
from openmontage.runner import cluster_pipeline
from openmontage.runner.worker import AlibabaOssObjectStoreClient, GrantStore, HeadlessAgent, PinnedManifestHeadlessAgent, Runner, RunnerConfig, StageExecutor, create_app


_FIXTURE_ROOTS = (
    Path("/contracts/openmontage/v1"),
    Path(__file__).resolve().parents[2] / "contracts" / "openmontage" / "v1",
    Path(__file__).resolve().parents[3] / "contracts" / "openmontage" / "v1",
)
ROOT = next((root for root in _FIXTURE_ROOTS if root.is_dir()), _FIXTURE_ROOTS[0])


def fixture(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def test_shared_fixtures_are_strictly_accepted():
    Command.model_validate(fixture("valid-command.json"))
    Event.model_validate(fixture("valid-event-approval-required.json"))
    Event.model_validate(fixture("valid-event-succeeded.json"))
    Grant.model_validate(fixture("valid-grant.json"))
    Cancel.model_validate(fixture("valid-cancel.json"))


def test_unknown_fields_and_credentials_are_rejected():
    unknown = fixture("invalid-unknown-field-command.json")
    credential = fixture("invalid-credential-command.json")
    with pytest.raises(ValidationError):
        Command.model_validate(unknown)
    with pytest.raises(ValidationError):
        Command.model_validate(credential)


def test_user_intent_accepts_public_reference_and_rejects_sensitive_locator():
    command = Command.model_validate(fixture("valid-command.json"))
    intent = UserIntent.model_validate({
        "brief": "Make a concise product launch video",
        "sources": [
            {"kind": "prompt"},
            {"kind": "reference_video", "locator": "https://www.youtube.com/watch?v=example"},
        ],
        "production": {
            "durationSeconds": 30,
            "aspectRatio": "16:9",
            "resolution": "1080p",
            "language": "en-US",
            "voice": "neutral",
            "subtitleStyle": "clean",
            "music": "auto",
            "visualStyle": "cinematic",
            "budgetTier": "balanced",
        },
    })
    command = command.model_copy(update={"userIntent": intent})
    assert command.userIntent.sources[1].locator.startswith("https://www.youtube.com")
    with pytest.raises(ValidationError):
        UserIntent.model_validate({**intent.model_dump(), "sources": [
            {"kind": "reference_video", "locator": "https://example.test/?x-amz-signature=secret"},
        ]})


def test_runner_identity_is_idempotent_and_pool_is_frozen():
    command = Command.model_validate(fixture("valid-command.json"))
    runner = Runner(RunnerConfig(channel="stable", pool="openmontage-planner", namespace="openmontage-test"))
    first = runner.handle(command)
    second = runner.handle(command)
    assert runner.identity(command) == "job-montage-0001:1:1:script"
    assert first.stage == second.stage == "script"
    assert first.workerPool == second.workerPool == "openmontage-planner"


def test_worker_classifies_grant_denial_as_typed_failure():
    command = Command.model_validate(fixture("valid-command.json"))
    runner = Runner(RunnerConfig(require_grants=True))
    event = runner._failure(command, PermissionError("task_grant_denied"))
    assert event.type == "TaskFailed"
    assert event.failure.code == "TASK_GRANT_DENIED"
    assert event.failure.retryable is False


def test_ready_endpoint_fails_while_draining():
    runner = Runner(RunnerConfig())
    app = create_app(runner)
    assert app is not None
    runner.drain()
    response = next(route.endpoint() for route in app.routes if getattr(route, "path", "") == "/readyz")
    assert response.status_code == 503
    assert response.body == b'{"ok":false}'


def test_stage_executor_verifies_checkpoint_receipt_and_task_grant(tmp_path):
    command = Command.model_validate(fixture("valid-command.json"))
    runner = Runner(RunnerConfig(workspace_root=str(tmp_path)))
    execution = runner.executor.execute(command)
    assert execution.checkpoint.sha256.startswith("sha256:")
    assert runner.executor.object_store.head(execution.checkpoint)
    grant = Grant.model_validate(fixture("valid-grant.json"))
    store = GrantStore()
    store.add(grant)
    with pytest.raises(PermissionError):
        store.authorize(command, "PUT", "other-task/object", 1)


class _PublishAgent(HeadlessAgent):
    def run(self, command, workspace):
        output = workspace / "renders" / "final.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"preview-video")
        return b'{"stage":"publish"}'


def test_publish_stage_uploads_preview_artifact_and_emits_task_success(tmp_path):
    command = Command.model_validate(fixture("valid-command.json")).model_copy(update={"stage": "publish"})
    config = RunnerConfig(workspace_root=str(tmp_path))
    executor = StageExecutor(config, agent=_PublishAgent())
    event = Runner(config, executor=executor).handle(command)
    assert event.type == "TaskSucceeded"
    assert event.artifacts[0].name == "final.mp4"
    assert event.artifacts[0].contentType == "video/mp4"
    assert event.artifacts[0].objectKey.endswith("/artifacts/final.mp4")
    assert "preview-video" not in event.model_dump_json()


def test_publish_stage_without_media_is_non_retryable_failure(tmp_path):
    command = Command.model_validate(fixture("valid-command.json")).model_copy(update={"stage": "publish"})
    config = RunnerConfig(workspace_root=str(tmp_path))
    runner = Runner(config)
    with pytest.raises(RuntimeError, match="publish_artifact_missing"):
        runner.handle(command)
    event = runner._failure(command, RuntimeError("publish_artifact_missing"))
    assert event.type == "TaskFailed"
    assert event.failure.code == "PUBLISH_ARTIFACT_MISSING"
    assert event.failure.retryable is False


def test_cluster_pipeline_publish_generates_and_verifies_video(tmp_path, monkeypatch):
    command = Command.model_validate(fixture("valid-command.json")).model_copy(update={
        "stage": "publish",
        "userIntent": UserIntent.model_validate({
            "brief": "A singer performing on a small stage",
            "sources": [{"kind": "prompt"}],
            "production": {
                "durationSeconds": 5,
                "aspectRatio": "16:9",
                "resolution": "720p",
                "language": "en-US",
                "voice": "neutral",
                "subtitleStyle": "none",
                "music": "none",
                "visualStyle": "cinematic",
                "budgetTier": "economy",
            },
        }),
    })
    calls = []

    class _Result:
        success = True

    class _Sora:
        def execute(self, inputs):
            calls.append(inputs)
            Path(inputs["output_path"]).write_bytes(b"video")
            return _Result()

    monkeypatch.setattr(cluster_pipeline, "SoraVideo", _Sora)
    monkeypatch.setattr(cluster_pipeline, "_apply_production_contract", lambda output, intent, workspace: {
        "requested": intent.production.model_dump(),
        "actual": {"durationSeconds": 5.0, "width": 1280, "height": 720, "hasAudio": False},
    })
    payload = json.loads(cluster_pipeline.run(command, tmp_path).decode("utf-8"))
    assert calls[0]["model"] == "sora-2"
    assert calls[0]["seconds"] == "8"
    assert calls[0]["size"] == "1280x720"
    assert "720p delivery resolution" in calls[0]["prompt"]
    assert "source modes as inputs where available: prompt" in calls[0]["prompt"]
    assert "Narration language: en-US" in calls[0]["prompt"]
    assert "Do not add subtitles" in calls[0]["prompt"]
    assert "Do not add background music" in calls[0]["prompt"]
    assert "Budget tier: economy" in calls[0]["prompt"]
    assert (tmp_path / "renders" / "final.mp4").is_file()
    assert payload["result"] == {
        "contentType": "video/mp4",
        "model": "sora-2",
        "name": "final.mp4",
        "provider": "openai",
        "route": "/v1/videos",
        "production": {
            "requested": command.userIntent.production.model_dump(),
            "actual": {"durationSeconds": 5.0, "width": 1280, "height": 720, "hasAudio": False},
        },
    }
    assert "A singer" not in json.dumps(payload)


def test_cluster_pipeline_passes_uploaded_image_to_sora(tmp_path, monkeypatch):
    image = tmp_path / "inputs" / "source-1.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"png-reference")
    command = Command.model_validate(fixture("valid-command.json")).model_copy(update={
        "stage": "publish",
        "userIntent": UserIntent.model_validate({
            "brief": "A singer performing on a small stage",
            "sources": [{"kind": "prompt"}, {"kind": "image", "objectKey": "uploads/ref.png",
                         "sha256": "sha256:" + "a" * 64, "sizeBytes": 13}],
            "production": {"durationSeconds": 4, "aspectRatio": "16:9", "resolution": "720p",
                           "language": "en-US", "voice": "neutral", "subtitleStyle": "none",
                           "music": "none", "visualStyle": "cinematic", "budgetTier": "economy"},
        }),
    })
    (tmp_path / "inputs" / "manifest.json").write_text(json.dumps([{"kind": "image", "path": str(image)}]))
    calls = []

    class _Result:
        success = True

    class _Sora:
        def execute(self, inputs):
            calls.append(inputs)
            Path(inputs["output_path"]).write_bytes(b"video")
            return _Result()

    monkeypatch.setattr(cluster_pipeline, "SoraVideo", _Sora)
    monkeypatch.setattr(cluster_pipeline, "_apply_production_contract", lambda output, intent, workspace: {
        "requested": intent.production.model_dump(),
        "actual": {"durationSeconds": 4.0, "width": 1280, "height": 720, "hasAudio": False},
    })
    cluster_pipeline.run(command, tmp_path)
    assert calls[0]["operation"] == "image_to_video"
    assert calls[0]["input_reference_path"] == str(image)


def test_cluster_pipeline_provider_failure_is_sanitized(tmp_path, monkeypatch):
    command = Command.model_validate(fixture("valid-command.json")).model_copy(update={
        "stage": "publish",
        "userIntent": UserIntent.model_validate({
            "brief": "A short test clip",
            "sources": [{"kind": "prompt"}],
            "production": {
                "durationSeconds": 4,
                "aspectRatio": "9:16",
                "resolution": "720p",
                "language": "en-US",
                "voice": "neutral",
                "subtitleStyle": "none",
                "music": "none",
                "visualStyle": "cinematic",
                "budgetTier": "economy",
            },
        }),
    })

    class _Result:
        success = False
        error = "provider secret https://example.test/signed"

    class _Sora:
        def execute(self, inputs):
            return _Result()

    monkeypatch.setattr(cluster_pipeline, "SoraVideo", _Sora)
    with pytest.raises(RuntimeError, match="^video_generation_failed$"):
        cluster_pipeline.run(command, tmp_path)


def test_production_contract_maps_requested_dimensions_and_subtitle_styles(tmp_path):
    assert cluster_pipeline._target_dimensions("9:16", "1080p") == (1080, 1920)
    assert cluster_pipeline._target_dimensions("16:9", "720p") == (1280, 720)
    assert cluster_pipeline._target_dimensions("1:1", "4k") == (2160, 2160)

    words = ["Make", "a", "short", "English", "product", "video"]
    srt = cluster_pipeline._render_srt(words, 8)
    assert "00:00:00,000 --> 00:00:08,000" in srt
    assert "Make a short English product video" in srt

    ass_path = tmp_path / "captions.ass"
    cluster_pipeline._write_ass(ass_path, words, 8)
    assert "\\k133" in ass_path.read_text(encoding="utf-8")


def test_production_contract_builds_real_normalization_command(tmp_path, monkeypatch):
    output = tmp_path / "renders" / "final.mp4"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"source")
    narration = tmp_path / "audio" / "narration.mp3"
    narration.parent.mkdir(parents=True)
    narration.write_bytes(b"audio")
    subtitle = tmp_path / "captions.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:04,000\nhello\n", encoding="utf-8")
    intent = UserIntent.model_validate({
        "brief": "hello product",
        "sources": [{"kind": "prompt"}],
        "production": {
            "durationSeconds": 4,
            "aspectRatio": "9:16",
            "resolution": "1080p",
            "language": "en-US",
            "voice": "female-warm",
            "subtitleStyle": "clean",
            "music": "none",
            "visualStyle": "product",
            "budgetTier": "economy",
        },
    })
    calls = []

    monkeypatch.setattr(cluster_pipeline.shutil, "which", lambda name: name)
    probe_calls = []

    def fake_probe(path, ffprobe):
        probe_calls.append(path)
        if len(probe_calls) == 1:
            return {"durationSeconds": 8.0, "width": 1280, "height": 720, "hasAudio": True}
        return {"durationSeconds": 4.0, "width": 1080, "height": 1920, "hasAudio": True}

    monkeypatch.setattr(cluster_pipeline, "_probe_video", fake_probe)
    monkeypatch.setattr(cluster_pipeline, "_generate_narration", lambda intent, workspace: narration)
    monkeypatch.setattr(cluster_pipeline, "_write_subtitle_asset", lambda intent, workspace, duration: subtitle)

    def fake_run(command, **kwargs):
        calls.append(command)
        Path(command[-1]).write_bytes(b"normalized")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cluster_pipeline.subprocess, "run", fake_run)
    result = cluster_pipeline._apply_production_contract(output, intent, tmp_path)
    command = calls[0]
    assert command[command.index("-threads") + 1] == "1"
    assert command[command.index("-filter_threads") + 1] == "1"
    assert command[command.index("-preset") + 1] == "ultrafast"
    assert "scale=1080:1920" in " ".join(command)
    assert "subtitles='" in " ".join(command)
    assert "-map" in command and "[narration]" in command
    assert result["actual"]["width"] == 1080
    assert result["actual"]["height"] == 1920
    assert result["actual"]["subtitleEmbedded"] is True
    assert result["actual"]["narrationVoice"] == "nova"


def test_heartbeat_event_preserves_attempt_identity():
    command = Command.model_validate(fixture("valid-command.json"))
    runner = Runner(RunnerConfig(channel="stable", pool="openmontage-planner"))
    event = runner._heartbeat(command)
    assert event.type == "Heartbeat"
    assert event.runId == command.runId
    assert event.attempt == command.attempt
    assert event.runRevision == command.runRevision


def test_inline_run_spec_skips_get_grant_and_writes_user_intent(tmp_path):
    command = Command.model_validate(fixture("valid-command.json")).model_copy(update={
        "runSpecRef": Command.model_validate(fixture("valid-command.json")).runSpecRef.model_copy(update={"inline": True}),
        "userIntent": UserIntent.model_validate({
            "brief": "Make a concise product launch video",
            "sources": [{"kind": "prompt"}],
            "production": {
                "durationSeconds": 5,
                "aspectRatio": "16:9",
                "resolution": "720p",
                "language": "en-US",
                "voice": "neutral",
                "subtitleStyle": "none",
                "music": "none",
                "visualStyle": "cinematic",
                "budgetTier": "economy",
            },
        }),
    })
    checkpoint_key = f"montage/{command.taskId}/run-{command.runRevision}/checkpoints/{command.stage}.json"
    store = GrantStore()
    expires = datetime.now(timezone.utc) + timedelta(minutes=5)
    for method in ("PUT", "HEAD"):
        store.add(Grant.model_validate({
            "schemaVersion": "openmontage.grant.v1",
            "grantId": f"grant-inline-{method.lower()}",
            "jobId": command.jobId,
            "attempt": command.attempt,
            "runRevision": command.runRevision,
            "stage": command.stage,
            "method": method,
            "expiresAt": expires.isoformat(),
            "targets": [{"name": "checkpoint", "objectKey": checkpoint_key,
                          "maxBytes": command.limits.maxOutputBytes, "contentType": "application/json"}],
        }))
    config = RunnerConfig(require_grants=True, workspace_root=str(tmp_path))
    executor = StageExecutor(config, grants=store)
    execution = executor.execute(command)
    run_spec = tmp_path / command.jobId / "revision-1" / command.stage / "attempt-1" / "run-spec.json"
    assert json.loads(run_spec.read_text(encoding="utf-8"))["brief"] == command.userIntent.brief
    assert execution.checkpoint.objectKey == checkpoint_key


def test_oss_get_supports_sdk_v2_stream_body_reader():
    body = b"{}"

    class _Reader:
        def read(self):
            return body

    class _GetResult:
        body = _Reader()

    class _Client:
        def get_object(self, request):
            return _GetResult()

    class _Sdk:
        class GetObjectRequest:
            def __init__(self, **kwargs):
                pass

    client = AlibabaOssObjectStoreClient.__new__(AlibabaOssObjectStoreClient)
    client.bucket = "test-bucket"
    client._client = _Client()
    client._oss = _Sdk()
    ref = Command.model_validate(fixture("valid-command.json")).runSpecRef.model_copy(
        update={"sizeBytes": len(body), "sha256": "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"})
    assert client.get(ref, max_bytes=10) == body


def test_grants_are_isolated_by_method():
    command = Command.model_validate(fixture("valid-command.json"))
    grant = Grant.model_validate(fixture("valid-grant.json")).model_copy(
        update={"expiresAt": datetime.now(timezone.utc) + timedelta(minutes=5)})
    store = GrantStore()
    store.add(grant)
    store.authorize(command, "GET", command.runSpecRef.objectKey, command.runSpecRef.sizeBytes)
    with pytest.raises(PermissionError):
        store.authorize(command, "HEAD", command.runSpecRef.objectKey, command.runSpecRef.sizeBytes)


def test_broadcast_group_is_unique_per_worker_pod():
    first = RunnerConfig(channel="stable", pool="openmontage-planner", pod_uid="pod-a")
    second = RunnerConfig(channel="stable", pool="openmontage-planner", pod_uid="pod-b")
    assert first.group == second.group
    assert first.broadcast_group != second.broadcast_group


class _FakeOssResult:
    def __init__(self, body=b"", metadata=None):
        self.body = __import__("io").BytesIO(body)
        self.content_length = len(body)
        self.metadata = metadata or {}


class _FakeOssClient:
    def __init__(self):
        self.objects = {}

    def put_object(self, request):
        body = request.body.read()
        self.objects[request.key] = (body, request.metadata)
        return object()

    def head_object(self, request):
        body, metadata = self.objects[request.key]
        return _FakeOssResult(body, metadata)

    def get_object(self, request):
        body, metadata = self.objects[request.key]
        return _FakeOssResult(body, metadata)


class _FakeOssSdk:
    class _Request:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    PutObjectRequest = _Request
    HeadObjectRequest = _Request
    GetObjectRequest = _Request


def test_alibaba_oss_adapter_put_head_get_round_trip():
    fake = _FakeOssClient()
    client = AlibabaOssObjectStoreClient("test-bucket", "cn-hangzhou", client=fake, sdk=_FakeOssSdk)
    body = b"checkpoint"
    digest = "sha256:" + __import__("hashlib").sha256(body).hexdigest()
    ref = client.put("montage/task/run-1/checkpoints/script.json", body, sha256=digest, max_bytes=1024)
    assert client.head(ref)
    assert client.get(ref, max_bytes=1024) == body


def test_pinned_manifest_agent_runs_declared_python_entrypoint(tmp_path):
    command = Command.model_validate(fixture("valid-command.json"))
    bundle = tmp_path / command.manifestVersion.removeprefix("sha256:")
    bundle.mkdir()
    (bundle / "manifest.json").write_text(json.dumps({
        "manifestVersion": command.manifestVersion,
        "stages": {"script": {"entrypoint": "bundle_stage:run"}},
    }), encoding="utf-8")
    (bundle / "bundle_stage.py").write_text(
        "def run(command, workspace):\n    return command.stage.encode()\n", encoding="utf-8")
    try:
        assert PinnedManifestHeadlessAgent(str(tmp_path)).run(command, tmp_path / "work") == b"script"
    finally:
        sys.modules.pop("bundle_stage", None)
