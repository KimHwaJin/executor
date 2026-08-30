"""Convert Runtime files and manifest entries into Artifact descriptors."""

from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from executor_service.domain.enums import (
    ArtifactStatus,
    ArtifactStorageType,
    ArtifactType,
)
from executor_service.domain.errors import ArtifactRegistrationError
from executor_service.domain.runtime import RuntimeFileMetadata, RuntimeStorage
from executor_service.infrastructure._artifacts.models import (
    ArtifactDescriptor,
    ArtifactManifestEntry,
)
from executor_service.infrastructure._artifacts.validation import (
    manifest_runtime_path,
    redact,
    validate_s3_uri,
)
from executor_service.infrastructure.workspace import ExecutionWorkspace


def runtime_file_descriptor(
    file: RuntimeFileMetadata,
    artifact_type: ArtifactType,
    status: ArtifactStatus,
    *,
    name: str | None = None,
    description: str | None = None,
    media_type: str | None = None,
    metadata: dict[str, Any],
    parent_artifact_id: UUID | None = None,
    external_parent_asset_id: str | None = None,
) -> ArtifactDescriptor:
    return ArtifactDescriptor(
        artifact_type=artifact_type,
        storage_type=ArtifactStorageType.PV,
        status=status,
        name=name or file.name,
        description=description,
        uri=f"jupyter-pv:///{file.path}",
        relative_path=file.path,
        media_type=media_type or file.media_type,
        size_bytes=file.size_bytes,
        checksum_sha256=file.checksum_sha256,
        parent_artifact_id=parent_artifact_id,
        external_parent_asset_id=external_parent_asset_id,
        metadata=redact({**metadata, "verification": "runtime-computed"}),
    )


async def manifest_descriptor(
    driver: RuntimeStorage,
    workspace: ExecutionWorkspace,
    entry: ArtifactManifestEntry,
    status: ArtifactStatus,
) -> ArtifactDescriptor:
    if entry.storage_type == ArtifactStorageType.PV:
        if entry.path is None:
            raise ArtifactRegistrationError("PV manifest path is missing.")
        metadata = await driver.file_metadata(
            manifest_runtime_path(entry.path, workspace)
        )
        return runtime_file_descriptor(
            metadata,
            entry.artifact_type,
            status,
            name=entry.name,
            description=entry.description,
            media_type=entry.media_type,
            metadata=entry.metadata,
            parent_artifact_id=entry.parent_artifact_id,
            external_parent_asset_id=entry.external_parent_asset_id,
        )
    if entry.uri is None:
        raise ArtifactRegistrationError("S3 manifest uri is missing.")
    validate_s3_uri(entry.uri)
    return ArtifactDescriptor(
        artifact_type=entry.artifact_type,
        storage_type=ArtifactStorageType.S3,
        status=status,
        name=(
            entry.name
            or PurePosixPath(urlsplit(entry.uri).path).name
            or "s3-artifact"
        ),
        description=entry.description,
        uri=entry.uri,
        relative_path=None,
        media_type=entry.media_type,
        size_bytes=entry.size_bytes,
        checksum_sha256=(
            entry.checksum_sha256.lower() if entry.checksum_sha256 else None
        ),
        parent_artifact_id=entry.parent_artifact_id,
        external_parent_asset_id=entry.external_parent_asset_id,
        metadata=redact(
            {**entry.metadata, "verification": "manifest-declared"}
        ),
    )
