"""Internal Artifact discovery and manifest models."""

from dataclasses import dataclass
from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from executor_service.domain.enums import (
    ArtifactStatus,
    ArtifactStorageType,
    ArtifactType,
)


@dataclass(frozen=True, slots=True)
class ArtifactDescriptor:
    artifact_type: ArtifactType
    storage_type: ArtifactStorageType
    status: ArtifactStatus
    name: str
    description: str | None
    uri: str
    relative_path: str | None
    media_type: str | None
    size_bytes: int | None
    checksum_sha256: str | None
    parent_artifact_id: UUID | None
    external_parent_asset_id: str | None
    metadata: dict[str, Any]


class ArtifactManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    storage_type: ArtifactStorageType
    artifact_type: ArtifactType
    path: str | None = None
    uri: str | None = None
    name: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=4000)
    media_type: str | None = Field(default=None, max_length=255)
    size_bytes: int | None = Field(default=None, ge=0)
    checksum_sha256: str | None = Field(
        default=None, pattern=r"^[a-fA-F0-9]{64}$"
    )
    parent_artifact_id: UUID | None = None
    external_parent_asset_id: str | None = Field(default=None, max_length=255)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_location(self) -> Self:
        if self.storage_type == ArtifactStorageType.PV:
            if self.path is None or self.uri is not None:
                raise ValueError("PV Artifact requires path and forbids uri.")
        elif self.uri is None or self.path is not None:
            raise ValueError("S3 Artifact requires uri and forbids path.")
        if self.storage_type == ArtifactStorageType.S3 and (
            self.size_bytes is None or self.checksum_sha256 is None
        ):
            raise ValueError(
                "S3 Artifact requires size_bytes and checksum_sha256."
            )
        return self
