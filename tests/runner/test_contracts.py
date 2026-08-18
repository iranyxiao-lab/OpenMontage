import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from openmontage.runner.contracts import Cancel, Command, Event, Grant
from openmontage.runner.worker import AlibabaOssObjectStoreClient, GrantStore, PinnedManifestHeadlessAgent, Runner, RunnerConfig, StageExecutor, create_app


ROOT = Path(__file__).resolve().parents[3] / "contracts" / "openmontage" / "v1"


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


def test_runner_identity_is_idempotent_and_pool_is_frozen():
    command = Command.model_validate(fixture("valid-command.json"))
    runner = Runner(RunnerConfig(channel="stable", pool="openmontage-planner", namespace="openmontage-test"))
    first = runner.handle(command)
    second = runner.handle(command)
    assert runner.identity(command) == "job-montage-0001:1:1:script"
    assert first.stage == second.stage == "script"
    assert first.workerPool == second.workerPool == "openmontage-planner"


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


def test_grants_are_isolated_by_method():
    command = Command.model_validate(fixture("valid-command.json"))
    grant = Grant.model_validate(fixture("valid-grant.json"))
    store = GrantStore()
    store.add(grant)
    store.authorize(command, "GET", command.runSpecRef.objectKey, command.runSpecRef.sizeBytes)
    with pytest.raises(PermissionError):
        store.authorize(command, "HEAD", command.runSpecRef.objectKey, command.runSpecRef.sizeBytes)


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
