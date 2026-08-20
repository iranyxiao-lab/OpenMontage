from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Ref(StrictModel):
    objectKey: str = Field(min_length=1, max_length=1024)
    sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    sizeBytes: int = Field(gt=0, le=8 * 1024**3)
    inline: bool = False

    @field_validator("objectKey")
    @classmethod
    def safe_key(cls, value: str) -> str:
        if value.startswith("/") or "://" in value or "\\" in value or "?" in value or "#" in value:
            raise ValueError("unsafe object key")
        if any(part in ("", ".", "..") for part in value.split("/")):
            raise ValueError("unsafe object key")
        return value


class Routing(StrictModel):
    channel: str = Field(pattern=r"^(stable|canary)$")
    workerPool: str = Field(pattern=r"^openmontage-(planner|render|gpu)$")
    executionMode: str = Field(pattern=r"^WORKER$")


class Limits(StrictModel):
    wallTimeoutSeconds: int = Field(gt=0, le=86400)
    maxOutputBytes: int = Field(gt=0, le=8 * 1024**3)


class Trace(StrictModel):
    traceId: str = Field(pattern=r"^[a-f0-9]{32}$")
    spanId: str = Field(pattern=r"^[a-f0-9]{16}$")


class Source(StrictModel):
    kind: str = Field(pattern=r"^[a-z][a-z_]{1,31}$")
    locator: str | None = Field(default=None, max_length=2048)
    # Uploaded task media is addressed by an immutable object reference.  The
    # locator remains a display/trace hint; workers use these fields for a
    # grant-scoped OSS GET and never pass oss:// locators to providers.
    objectKey: str | None = Field(default=None, min_length=1, max_length=1024)
    sha256: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")
    sizeBytes: int | None = Field(default=None, gt=0, le=8 * 1024**3)

    @field_validator("locator")
    @classmethod
    def safe_locator(cls, value: str | None) -> str | None:
        if value is not None and any(marker in value.lower() for marker in (
            "authorization", "bearer ", "cookie", "secret", "access_key", "accesskey", "signature", "x-amz-"
        )):
            raise ValueError("sensitive material is not allowed")
        return value

    @model_validator(mode="after")
    def validate_object_ref(self) -> "Source":
        present = (self.objectKey is not None, self.sha256 is not None, self.sizeBytes is not None)
        if any(present) and not all(present):
            raise ValueError("source object reference must be complete")
        if self.objectKey is not None:
            if self.objectKey.startswith(("/", "\\")) or "://" in self.objectKey or any(
                part in ("", ".", "..") for part in self.objectKey.split("/")
            ):
                raise ValueError("unsafe source object key")
        return self


class ProductionSettings(StrictModel):
    durationSeconds: int = Field(gt=0, le=86400)
    aspectRatio: str = Field(pattern=r"^(16:9|9:16|1:1)$")
    resolution: str = Field(pattern=r"^(720p|1080p|4k)$")
    language: str = Field(pattern=r"^[A-Za-z-]{2,16}$")
    voice: str = Field(pattern=r"^[a-z0-9-]{1,64}$")
    subtitleStyle: str = Field(pattern=r"^(none|clean|karaoke|highlight)$")
    music: str = Field(pattern=r"^(none|auto|provided)$")
    visualStyle: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")
    budgetTier: str = Field(pattern=r"^(economy|balanced|premium)$")
    model: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )


class UserIntent(StrictModel):
    brief: str = Field(min_length=1, max_length=4000)
    sources: list[Source] = Field(min_length=1, max_length=16)
    production: ProductionSettings


class Command(StrictModel):
    schemaVersion: str = Field(pattern=r"^openmontage\.command\.v1$")
    jobId: str = Field(pattern=r"^[A-Za-z0-9._-]{1,128}$")
    taskId: str = Field(pattern=r"^[A-Za-z0-9._-]{1,128}$")
    bizTaskId: str = Field(pattern=r"^[A-Za-z0-9._-]{1,128}$")
    runId: str = Field(pattern=r"^[A-Za-z0-9._-]{1,128}$")
    attempt: int = Field(ge=1, le=100)
    runRevision: int = Field(ge=1, le=1000)
    stage: str = Field(pattern=r"^[a-z][a-z_]{0,31}$")
    pipelineType: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    manifestVersion: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    runSpecRef: Ref
    inputPlanId: str = Field(pattern=r"^[A-Za-z0-9._-]{1,128}$")
    outputPlanId: str = Field(pattern=r"^[A-Za-z0-9._-]{1,128}$")
    routing: Routing
    limits: Limits
    trace: Trace
    userIntent: UserIntent | None = None

    @model_validator(mode="after")
    def no_sensitive_material(self) -> "Command":
        reject_sensitive(self.model_dump())
        return self


class Artifact(StrictModel):
    name: str = Field(pattern=r"^[A-Za-z0-9._-]{1,128}$")
    objectKey: str
    contentType: str = Field(min_length=1, max_length=128)
    sizeBytes: int = Field(gt=0, le=8 * 1024**3)
    eTag: str = Field(min_length=1, max_length=256)

    @field_validator("objectKey")
    @classmethod
    def safe_artifact_key(cls, value: str) -> str:
        if value.startswith("/") or "://" in value or "\\" in value or any(part in ("", ".", "..") for part in value.split("/")):
            raise ValueError("unsafe object key")
        return value


class Failure(StrictModel):
    code: str = Field(pattern=r"^[A-Z0-9_]{1,64}$")
    retryable: bool
    summary: str = Field(max_length=1024)


class Event(StrictModel):
    schemaVersion: str = Field(pattern=r"^openmontage\.event\.v1$")
    eventId: str = Field(pattern=r"^[A-Za-z0-9._-]{1,128}$")
    jobId: str
    taskId: str
    runId: str
    attempt: int = Field(ge=1, le=100)
    runRevision: int = Field(ge=1, le=1000)
    type: str
    occurredAt: datetime
    workerId: str = Field(min_length=1, max_length=192)
    workerPool: str
    channel: str = Field(pattern=r"^(stable|canary)$")
    stage: str
    checkpointRef: Ref | None = None
    artifacts: list[Artifact] | None = None
    failure: Failure | None = None


class GrantTarget(StrictModel):
    name: str
    objectKey: str
    maxBytes: int = Field(gt=0, le=8 * 1024**3)
    contentType: str

    @field_validator("objectKey")
    @classmethod
    def safe_grant_key(cls, value: str) -> str:
        if value.startswith("/") or "://" in value or "\\" in value or any(part in ("", ".", "..") for part in value.split("/")):
            raise ValueError("unsafe object key")
        return value


class Grant(StrictModel):
    schemaVersion: str = Field(pattern=r"^openmontage\.grant\.v1$")
    grantId: str
    jobId: str
    attempt: int = Field(ge=1, le=100)
    runRevision: int = Field(ge=1, le=1000)
    stage: str
    method: str = Field(pattern=r"^(GET|PUT|HEAD)$")
    expiresAt: datetime
    targets: list[GrantTarget] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def no_sensitive_material(self) -> "Grant":
        reject_sensitive(self.model_dump())
        return self


class Cancel(StrictModel):
    model_config = ConfigDict(extra="forbid")
    schemaVersion: str = Field(pattern=r"^openmontage\.cancel\.v1$")
    cancelId: str
    jobId: str
    taskId: str
    runId: str
    attempt: int = Field(ge=1, le=100)
    runRevision: int = Field(ge=1, le=1000)
    requestedAt: datetime
    reasonCode: str = Field(pattern=r"^[A-Z0-9_]{1,64}$")


def reject_sensitive(value: Any) -> None:
    text = str(value).lower()
    for marker in ("authorization", "bearer ", "accesskey", "secret", "x-amz-", "private_key"):
        if marker in text:
            raise ValueError("sensitive material is not allowed")
