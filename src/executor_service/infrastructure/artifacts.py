"""Secure Artifact discovery, manifest parsing, hashing, and persistence."""

import asyncio
import hashlib
import json
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.config import Settings
from executor_service.domain.enums import (
    ArtifactStatus,
    ArtifactStorageType,
    ArtifactType,
)
from executor_service.domain.errors import ArtifactRegistrationError
from executor_service.domain.models import OutboxEvent
from executor_service.infrastructure.db.models import (
    ExecutionArtifactORM,
    ExecutionStepAttemptORM,
    ExecutionStepORM,
    OutboxEventORM,
)
from executor_service.infrastructure.workspace import ExecutionWorkspace
from executor_service.tracing import capture_trace_carrier

MANIFEST_RELATIVE_PATH = Path("artifacts", "manifest.jsonl")
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
class FileState:
    size_bytes: int
    modified_ns: int


@dataclass(frozen=True, slots=True)
class ArtifactSnapshot:
    files: dict[Path, FileState]
    manifest_size: int


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
    checksum_sha256: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")
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
            raise ValueError("S3 Artifact requires size_bytes and checksum_sha256.")
        return self


class ExecutionArtifactManager:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._host_root = settings.workspace_host_root.resolve()
        self._runtime_root = Path(settings.workspace_runtime_root)

    def snapshot(self, workspace: ExecutionWorkspace) -> ArtifactSnapshot:
        files: dict[Path, FileState] = {}
        for path in workspace.artifacts_dir.rglob("*"):
            if path.is_file() and path != workspace.host_root / MANIFEST_RELATIVE_PATH:
                stat = path.stat()
                files[path.resolve()] = FileState(stat.st_size, stat.st_mtime_ns)
        manifest = workspace.host_root / MANIFEST_RELATIVE_PATH
        return ArtifactSnapshot(
            files=files,
            manifest_size=manifest.stat().st_size if manifest.is_file() else 0,
        )

    async def discover_and_register(
        self,
        *,
        workspace: ExecutionWorkspace,
        before: ArtifactSnapshot,
        execution_id: UUID,
        attempt_id: UUID,
        sequence: int,
        status: ArtifactStatus,
    ) -> list[UUID]:
        descriptors = await asyncio.to_thread(self._discover, workspace, before, status)
        return await self._persist(
            descriptors,
            execution_id=execution_id,
            attempt_id=attempt_id,
            sequence=sequence,
        )

    async def register_notebook(
        self,
        *,
        workspace: ExecutionWorkspace,
        execution_id: UUID,
        attempt_id: UUID,
        sequence: int,
    ) -> list[UUID]:
        descriptor = await asyncio.to_thread(
            self._pv_descriptor,
            workspace.notebook_file,
            ArtifactType.NOTEBOOK,
            ArtifactStatus.AVAILABLE,
            None,
            None,
            None,
            {},
        )
        return await self._persist(
            [descriptor],
            execution_id=execution_id,
            attempt_id=attempt_id,
            sequence=sequence,
        )

    def _discover(
        self,
        workspace: ExecutionWorkspace,
        before: ArtifactSnapshot,
        status: ArtifactStatus,
    ) -> list[ArtifactDescriptor]:
        manifest_descriptors = self._manifest_descriptors(workspace, before, status)
        manifest_uris = {descriptor.uri for descriptor in manifest_descriptors}
        current = self.snapshot(workspace)
        automatic: list[ArtifactDescriptor] = []
        for path, state in current.files.items():
            previous = before.files.get(path)
            if previous == state:
                continue
            artifact_type = _infer_artifact_type(path, workspace)
            descriptor = self._pv_descriptor(
                path,
                artifact_type,
                status,
                None,
                None,
                None,
                {"discovery": "workspace-diff"},
            )
            if descriptor.uri not in manifest_uris:
                automatic.append(descriptor)
        return [*manifest_descriptors, *automatic]

    def _manifest_descriptors(
        self,
        workspace: ExecutionWorkspace,
        before: ArtifactSnapshot,
        status: ArtifactStatus,
    ) -> list[ArtifactDescriptor]:
        manifest = workspace.host_root / MANIFEST_RELATIVE_PATH
        if not manifest.is_file():
            return []
        start = before.manifest_size
        if manifest.stat().st_size < start:
            start = 0
        with manifest.open("rb") as handle:
            handle.seek(start)
            raw_lines = handle.read().decode("utf-8").splitlines()
        descriptors: list[ArtifactDescriptor] = []
        for line_number, raw in enumerate(raw_lines, start=1):
            if not raw.strip():
                continue
            try:
                entry = ArtifactManifestEntry.model_validate_json(raw)
                descriptors.append(self._from_manifest(entry, workspace, status))
            except Exception as exc:
                raise ArtifactRegistrationError(
                    f"Invalid Artifact manifest entry near appended line {line_number}."
                ) from exc
        return descriptors

    def _from_manifest(
        self,
        entry: ArtifactManifestEntry,
        workspace: ExecutionWorkspace,
        status: ArtifactStatus,
    ) -> ArtifactDescriptor:
        if entry.storage_type == ArtifactStorageType.PV:
            if entry.path is None:
                raise ArtifactRegistrationError("PV manifest path is missing.")
            path = self._resolve_pv_path(entry.path, workspace)
            return self._pv_descriptor(
                path,
                entry.artifact_type,
                status,
                entry.name,
                entry.description,
                entry.media_type,
                entry.metadata,
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
            name=entry.name or Path(urlsplit(entry.uri).path).name or "s3-artifact",
            description=entry.description,
            uri=entry.uri,
            relative_path=None,
            media_type=entry.media_type,
            size_bytes=entry.size_bytes,
            checksum_sha256=(entry.checksum_sha256.lower() if entry.checksum_sha256 else None),
            parent_artifact_id=entry.parent_artifact_id,
            external_parent_asset_id=entry.external_parent_asset_id,
            metadata=_redact({**entry.metadata, "verification": "manifest-declared"}),
        )

    def _pv_descriptor(
        self,
        path: Path,
        artifact_type: ArtifactType,
        status: ArtifactStatus,
        name: str | None,
        description: str | None,
        media_type: str | None,
        metadata: dict[str, Any],
        *,
        parent_artifact_id: UUID | None = None,
        external_parent_asset_id: str | None = None,
    ) -> ArtifactDescriptor:
        resolved = path.resolve(strict=True)
        _ensure_within(resolved, self._host_root)
        if not resolved.is_file():
            raise ArtifactRegistrationError("PV Artifact path must be a file.")
        relative = resolved.relative_to(self._host_root).as_posix()
        return ArtifactDescriptor(
            artifact_type=artifact_type,
            storage_type=ArtifactStorageType.PV,
            status=status,
            name=name or resolved.name,
            description=description,
            uri=f"pv:///{relative}",
            relative_path=relative,
            media_type=media_type or mimetypes.guess_type(resolved.name)[0],
            size_bytes=resolved.stat().st_size,
            checksum_sha256=_sha256(resolved),
            parent_artifact_id=parent_artifact_id,
            external_parent_asset_id=external_parent_asset_id,
            metadata=_redact({**metadata, "verification": "executor-computed"}),
        )

    def _resolve_pv_path(self, raw: str, workspace: ExecutionWorkspace) -> Path:
        candidate = Path(raw)
        if candidate.is_absolute():
            try:
                relative = candidate.relative_to(self._runtime_root)
                candidate = self._host_root / relative
            except ValueError:
                pass
        elif candidate.parts and candidate.parts[0] == "users":
            candidate = self._host_root / candidate
        else:
            candidate = workspace.host_root / candidate
        resolved = candidate.resolve(strict=True)
        _ensure_within(resolved, self._host_root)
        return resolved

    async def _persist(
        self,
        descriptors: list[ArtifactDescriptor],
        *,
        execution_id: UUID,
        attempt_id: UUID,
        sequence: int,
    ) -> list[UUID]:
        if not descriptors:
            return []
        async with self._session_factory() as session, session.begin():
            step = await session.scalar(
                select(ExecutionStepORM).where(
                    ExecutionStepORM.execution_id == execution_id,
                    ExecutionStepORM.sequence == sequence,
                )
            )
            step_attempt = await session.scalar(
                select(ExecutionStepAttemptORM).where(
                    ExecutionStepAttemptORM.execution_attempt_id == attempt_id,
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
                    execution_id, attempt_id, step_attempt.id, descriptor
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
                    execution_id=execution_id,
                    execution_attempt_id=attempt_id,
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
                    created_by_type=step.updated_by_type or step.created_by_type,
                    created_by=step.updated_by or step.created_by,
                    updated_by_type=step.updated_by_type or step.created_by_type,
                    updated_by=step.updated_by or step.created_by,
                )
                session.add(row)
                await session.flush()
                artifact_ids.append(row.id)
                carrier = capture_trace_carrier()
                event = OutboxEvent(
                    aggregate_type="Execution",
                    aggregate_id=execution_id,
                    event_type="execution.artifact_registered",
                    payload={
                        "execution_id": str(execution_id),
                        "execution_attempt_id": str(attempt_id),
                        "execution_step_id": str(step.id),
                        "artifact_id": str(row.id),
                        "artifact_type": descriptor.artifact_type.value,
                        "storage_type": descriptor.storage_type.value,
                        "status": descriptor.status.value,
                        "uri": descriptor.uri,
                    },
                    created_by_type=row.created_by_type,
                    created_by=row.created_by,
                    updated_by_type=row.updated_by_type,
                    updated_by=row.updated_by,
                    traceparent=carrier.traceparent,
                    tracestate=carrier.tracestate,
                )
                session.add(OutboxEventORM.from_domain(event))
            return artifact_ids


def _infer_artifact_type(path: Path, workspace: ExecutionWorkspace) -> ArtifactType:
    try:
        relative = path.resolve().relative_to(workspace.artifacts_dir.resolve())
    except ValueError:
        relative = None
    if relative is not None and len(relative.parts) > 1:
        directory_type = ARTIFACT_DIRECTORY_TYPES.get(relative.parts[0])
        if directory_type is not None:
            return directory_type
    suffix = path.suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".svg", ".webp", ".pdf"}:
        return ArtifactType.PLOT
    if suffix in {".csv", ".parquet", ".feather", ".xlsx", ".npy", ".npz"}:
        return ArtifactType.DATASET
    if suffix in {".pkl", ".pickle", ".joblib", ".onnx", ".pt", ".pth", ".h5"}:
        return ArtifactType.MODEL
    if suffix == ".log":
        return ArtifactType.LOG
    if "metric" in path.stem.lower() and suffix in {".json", ".jsonl"}:
        return ArtifactType.METRIC
    return ArtifactType.OTHER


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.lstrip("/"):
        raise ArtifactRegistrationError("S3 Artifact uri must be s3://bucket/key.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ArtifactRegistrationError("S3 Artifact uri must not contain credentials or query.")


def _ensure_within(path: Path, root: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ArtifactRegistrationError("Artifact path escapes the configured PV root.") from exc


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
    return any(marker in normalized for marker in ("token", "secret", "password", "credential"))
