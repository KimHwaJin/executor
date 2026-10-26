"""Validated public contract for Executor Step Result Manifest 1.0."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ManifestIdentity(StrictModel):
    execution_id: UUID
    operation_id: UUID
    step_id: UUID
    sequence: int = Field(ge=0)
    execution_attempt_id: UUID
    fencing_token: int = Field(ge=1)


class ManifestSource(StrictModel):
    relative_path: str = Field(min_length=1)
    checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)


class ManifestRepresentation(StrictModel):
    media_type: str = Field(min_length=1)
    encoding: Literal["UTF8", "BASE64"]
    relative_path: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    complete: Literal[True]
    truncated_in_preview: Literal[False]
    metadata: dict[str, Any]


class ManifestOutput(StrictModel):
    ordinal: int = Field(ge=0)
    kind: Literal["STREAM", "DISPLAY", "RESULT", "ERROR"]
    stream_name: str | None
    execution_count: int | None
    representations: list[ManifestRepresentation]
    metadata: dict[str, Any]
    created_at: datetime


class ManifestOutputSummary(StrictModel):
    output_count: int = Field(ge=0)
    output_types: dict[str, int]
    stream_names: list[str]
    mime_types: list[str]
    has_image: bool
    image_count: int = Field(ge=0)
    has_error: bool


class StepResultManifest(StrictModel):
    schema_version: Literal["1.0"]
    state: Literal["FINALIZED", "FAILED", "ABORTED"]
    complete: bool
    identity: ManifestIdentity
    source: ManifestSource
    outputs: list[ManifestOutput]
    output_count: int = Field(ge=0)
    representation_count: int = Field(ge=0)
    total_size_bytes: int = Field(ge=0)
    execution_count: int | None
    error_message: str | None
    output_summary: ManifestOutputSummary
    created_at: datetime
    updated_at: datetime
    completed_at: datetime

    @model_validator(mode="after")
    def validate_terminal_state(self):
        if self.state == "ABORTED":
            if self.complete or not self.error_message:
                raise ValueError("ABORTED manifest must be incomplete with an error")
        else:
            if not self.complete:
                raise ValueError("FINALIZED/FAILED manifest must be complete")
        if self.state == "FINALIZED" and self.error_message is not None:
            raise ValueError("FINALIZED manifest cannot contain an error")
        if self.state != "FINALIZED" and not self.error_message:
            raise ValueError("FAILED/ABORTED manifest requires an error")
        return self


__all__ = ["StepResultManifest"]
