"""Runtime-backed Artifact discovery, manifest parsing, and persistence."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.domain.enums import (
    ArtifactStatus,
    ArtifactStorageType,
    ArtifactType,
    ExecutionStatus,
)
from executor_service.domain.errors import ArtifactRegistrationError
from executor_service.domain.runtime import (
    RuntimeFileMetadata,
    RuntimeFileState,
    RuntimeStorage,
    RuntimeStorageSnapshot,
)
from executor_service.infrastructure.db.models import (
    ExecutionArtifactORM,
    ExecutionStepAttemptORM,
    ExecutionStepORM,
)
from executor_service.infrastructure.execution_leases import (
    ExecutionLease,
    require_active_lease,
)
from executor_service.infrastructure.workspace import ExecutionWorkspace

ARTIFACT_DIRECTORY_TYPES = {
    "datasets": ArtifactType.DATASET,
    "plots": ArtifactType.PLOT,
    "models": ArtifactType.MODEL,
    "metrics": ArtifactType.METRIC,
    "reports": ArtifactType.REPORT,
    "logs": ArtifactType.LOG,
    "other": ArtifactType.OTHER,
}


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


class ExecutionArtifactManager:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        self._session_factory = session_factory

    async def snapshot(
        self, driver: RuntimeStorage, workspace: ExecutionWorkspace
    ) -> RuntimeStorageSnapshot:
        return await driver.artifact_snapshot(workspace.runtime_relative_path)

    async def discover_and_register(
        self,
        *,
        driver: RuntimeStorage,
        workspace: ExecutionWorkspace,
        before: RuntimeStorageSnapshot,
        lease: ExecutionLease,
        sequence: int,
        status: ArtifactStatus,
        allow_cancel_requested: bool = False,
    ) -> list[UUID]:
        descriptors = await self._discover(driver, workspace, before, status)
        return await self._persist(
            descriptors,
            lease=lease,
            sequence=sequence,
            allow_cancel_requested=allow_cancel_requested,
        )

    async def register_notebook(
        self,
        *,
        driver: RuntimeStorage,
        workspace: ExecutionWorkspace,
        lease: ExecutionLease,
        sequence: int,
    ) -> list[UUID]:
        metadata = await driver.file_metadata(workspace.notebook_path)
        descriptor = _runtime_file_descriptor(
            metadata,
            ArtifactType.NOTEBOOK,
            ArtifactStatus.AVAILABLE,
            metadata={},
        )
        return await self._persist(
            [descriptor],
            lease=lease,
            sequence=sequence,
        )

    async def _discover(
        self,
        driver: RuntimeStorage,
        workspace: ExecutionWorkspace,
        before: RuntimeStorageSnapshot,
        status: ArtifactStatus,
    ) -> list[ArtifactDescriptor]:
        manifest = await driver.read_manifest(
            workspace.runtime_relative_path, before.manifest_size
        )
        manifest_descriptors = await self._manifest_descriptors(
            driver, workspace, manifest, status
        )
        manifest_uris = {descriptor.uri for descriptor in manifest_descriptors}
        current = await driver.artifact_snapshot(
            workspace.runtime_relative_path
        )
        previous = {state.path: state for state in before.files}
        automatic: list[ArtifactDescriptor] = []
        for state in current.files:
            if previous.get(state.path) == state:
                continue
            artifact_type = _infer_artifact_type(state, workspace)
            if artifact_type is None:
                continue
            metadata = await driver.file_metadata(state.path)
            descriptor = _runtime_file_descriptor(
                metadata,
                artifact_type,
                status,
                metadata={"discovery": "runtime-workspace-diff"},
            )
            if descriptor.uri not in manifest_uris:
                automatic.append(descriptor)
        return [*manifest_descriptors, *automatic]

    async def _manifest_descriptors(
        self,
        driver: RuntimeStorage,
        workspace: ExecutionWorkspace,
        content: bytes,
        status: ArtifactStatus,
    ) -> list[ArtifactDescriptor]:
        try:
            lines = content.decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise ArtifactRegistrationError(
                "Artifact manifest must be UTF-8."
            ) from exc
        descriptors: list[ArtifactDescriptor] = []
        for line_number, raw in enumerate(lines, start=1):
            if not raw.strip():
                continue
            try:
                entry = ArtifactManifestEntry.model_validate_json(raw)
                descriptors.append(
                    await self._from_manifest(driver, workspace, entry, status)
                )
            except Exception as exc:
                raise ArtifactRegistrationError(
                    f"Invalid Artifact manifest entry near appended line {line_number}."
                ) from exc
        return descriptors

    async def _from_manifest(
        self,
        driver: RuntimeStorage,
        workspace: ExecutionWorkspace,
        entry: ArtifactManifestEntry,
        status: ArtifactStatus,
    ) -> ArtifactDescriptor:
        if entry.storage_type == ArtifactStorageType.PV:
            if entry.path is None:
                raise ArtifactRegistrationError("PV manifest path is missing.")
            metadata = await driver.file_metadata(
                _manifest_runtime_path(entry.path, workspace)
            )
            return _runtime_file_descriptor(
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
        _validate_s3_uri(entry.uri)
        return ArtifactDescriptor(
            artifact_type=entry.artifact_type,
            storage_type=ArtifactStorageType.S3,
            status=status,
            name=entry.name
            or PurePosixPath(urlsplit(entry.uri).path).name
            or "s3-artifact",
            description=entry.description,
            uri=entry.uri,
            relative_path=None,
            media_type=entry.media_type,
            size_bytes=entry.size_bytes,
            checksum_sha256=(
                entry.checksum_sha256.lower()
                if entry.checksum_sha256
                else None
            ),
            parent_artifact_id=entry.parent_artifact_id,
            external_parent_asset_id=entry.external_parent_asset_id,
            metadata=_redact(
                {**entry.metadata, "verification": "manifest-declared"}
            ),
        )

    async def _persist(
        self,
        descriptors: list[ArtifactDescriptor],
        *,
        lease: ExecutionLease,
        sequence: int,
        allow_cancel_requested: bool = False,
    ) -> list[UUID]:
        if not descriptors:
            return []
        async with self._session_factory() as session, session.begin():
            await require_active_lease(
                session,
                lease,
                allowed_statuses=(
                    (
                        ExecutionStatus.RUNNING,
                        ExecutionStatus.CANCEL_REQUESTED,
                    )
                    if allow_cancel_requested
                    else (ExecutionStatus.RUNNING,)
                ),
            )
            step = await session.scalar(
                select(ExecutionStepORM).where(
                    ExecutionStepORM.execution_id == lease.execution_id,
                    ExecutionStepORM.sequence == sequence,
                )
            )
            step_attempt = await session.scalar(
                select(ExecutionStepAttemptORM).where(
                    ExecutionStepAttemptORM.execution_attempt_id
                    == lease.attempt_id,
                    ExecutionStepAttemptORM.sequence == sequence,
                )
            )
            if step is None or step_attempt is None:
                raise ArtifactRegistrationError(
                    "Artifact cannot be linked because its Execution Step is missing."
                )
            artifact_ids: list[UUID] = []
            for descriptor in descriptors:
                identity_hash = _identity_hash(
                    lease.execution_id,
                    lease.attempt_id,
                    step_attempt.id,
                    descriptor,
                )
                existing = await session.scalar(
                    select(ExecutionArtifactORM).where(
                        ExecutionArtifactORM.identity_hash == identity_hash
                    )
                )
                if existing is not None:
                    artifact_ids.append(existing.id)
                    continue
                row = ExecutionArtifactORM(
                    execution_id=lease.execution_id,
                    execution_attempt_id=lease.attempt_id,
                    execution_step_id=step.id,
                    execution_step_attempt_id=step_attempt.id,
                    parent_artifact_id=descriptor.parent_artifact_id,
                    external_parent_asset_id=descriptor.external_parent_asset_id,
                    artifact_type=descriptor.artifact_type,
                    storage_type=descriptor.storage_type,
                    status=descriptor.status,
                    name=descriptor.name,
                    description=descriptor.description,
                    uri=descriptor.uri,
                    relative_path=descriptor.relative_path,
                    media_type=descriptor.media_type,
                    size_bytes=descriptor.size_bytes,
                    checksum_sha256=descriptor.checksum_sha256,
                    artifact_metadata=descriptor.metadata,
                    identity_hash=identity_hash,
                    created_by_type=step.updated_by_type
                    or step.created_by_type,
                    created_by=step.updated_by or step.created_by,
                    updated_by_type=step.updated_by_type
                    or step.created_by_type,
                    updated_by=step.updated_by or step.created_by,
                )
                session.add(row)
                await session.flush()
                artifact_ids.append(row.id)
            return artifact_ids


def _runtime_file_descriptor(
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
        metadata=_redact({**metadata, "verification": "runtime-computed"}),
    )


def _infer_artifact_type(
    state: RuntimeFileState, workspace: ExecutionWorkspace
) -> ArtifactType | None:
    path = PurePosixPath(state.path)
    root = PurePosixPath(workspace.artifacts_path)
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    if len(relative.parts) <= 1:
        return None
    return ARTIFACT_DIRECTORY_TYPES.get(relative.parts[0])


def _manifest_runtime_path(raw: str, workspace: ExecutionWorkspace) -> str:
    path = PurePosixPath(raw)
    if path.is_absolute() or (path.parts and path.parts[0] == "users"):
        return raw
    if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ArtifactRegistrationError(
            "PV manifest path contains an unsafe segment."
        )
    return f"{workspace.runtime_relative_path}/{path.as_posix()}"


def _identity_hash(
    execution_id: UUID,
    attempt_id: UUID,
    step_attempt_id: UUID,
    descriptor: ArtifactDescriptor,
) -> str:
    payload = {
        "execution_id": str(execution_id),
        "attempt_id": str(attempt_id),
        "step_attempt_id": str(step_attempt_id),
        "uri": descriptor.uri,
        "checksum_sha256": descriptor.checksum_sha256,
        "status": descriptor.status.value,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _validate_s3_uri(uri: str) -> None:
    parsed = urlsplit(uri)
    if (
        parsed.scheme != "s3"
        or not parsed.netloc
        or not parsed.path.lstrip("/")
    ):
        raise ArtifactRegistrationError(
            "S3 Artifact uri must be s3://bucket/key."
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ArtifactRegistrationError(
            "S3 Artifact uri must not contain credentials or query."
        )


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _is_secret_key(key) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _is_secret_key(key: str) -> bool:
    normalized = key.lower()
    return any(
        marker in normalized
        for marker in ("token", "secret", "password", "credential")
    )
