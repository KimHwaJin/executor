"""Materialize Agent-authored text as a Runtime-owned Execution Artifact."""

import asyncio
import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from uuid import UUID

import nbformat
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.application.commands import MaterializeArtifactCommand
from executor_service.domain.enums import (
    ArtifactStatus,
    ArtifactStorageType,
    ArtifactType,
    CodeSourceType,
    ExecutionStatus,
)
from executor_service.domain.errors import (
    ArtifactRegistrationError,
    ExecutionNotFoundError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
)
from executor_service.domain.runtime import RuntimeStorageAccess
from executor_service.events import build_execution_event
from executor_service.infrastructure.db.models import (
    CommandReceiptORM,
    ExecutionArtifactORM,
    ExecutionORM,
    OutboxEventORM,
)
from executor_service.tracing import capture_trace_carrier

SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
ARTIFACT_DIRECTORIES = {
    ArtifactType.DATASET: "datasets",
    ArtifactType.PLOT: "plots",
    ArtifactType.MODEL: "models",
    ArtifactType.METRIC: "metrics",
    ArtifactType.LOG: "logs",
    ArtifactType.OTHER: "other",
}


class MaterializedArtifactService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        runtime_storage: RuntimeStorageAccess,
        input_root: Path,
        *,
        max_bytes: int,
    ) -> None:
        self._session_factory = session_factory
        self._runtime_storage = runtime_storage
        self._input_root = input_root.resolve()
        self._max_bytes = max_bytes

    async def materialize(self, command: MaterializeArtifactCommand) -> UUID:
        fingerprint = _fingerprint(command)
        existing = await self._receipt(command.idempotency_key, fingerprint)
        if existing is not None:
            return existing
        content = await self._resolve_content(command)
        execution = await self._execution(command.execution_id)
        if execution.status != ExecutionStatus.SUCCEEDED:
            raise InvalidStateTransitionError(
                "Execution Artifacts authored after execution require SUCCEEDED state."
            )
        if execution.workspace_path is None:
            raise ArtifactRegistrationError(
                "Execution workspace is not available."
            )
        name = _artifact_name(command)
        target_path = _target_path(
            execution.workspace_path, command.artifact_type, name
        )
        file = await self._runtime_storage.write_text(
            execution.runtime_type,
            execution.runtime_target_id,
            target_path,
            content,
        )
        if command.append_to_notebook:
            await self._append_notebook(
                execution, command.idempotency_key, content
            )

        artifact_id = UUID(
            bytes=hashlib.sha256(fingerprint.encode()).digest()[:16]
        )
        identity_hash = hashlib.sha256(
            f"{command.execution_id}:{target_path}:{file.checksum_sha256}".encode()
        ).hexdigest()
        carrier = capture_trace_carrier()
        async with self._session_factory() as session, session.begin():
            repeated = await session.scalar(
                select(CommandReceiptORM).where(
                    CommandReceiptORM.idempotency_key
                    == command.idempotency_key
                )
            )
            if repeated is not None:
                return _validate_receipt(repeated, fingerprint)
            artifact = await session.scalar(
                select(ExecutionArtifactORM).where(
                    ExecutionArtifactORM.identity_hash == identity_hash
                )
            )
            created = artifact is None
            if artifact is None:
                artifact = ExecutionArtifactORM(
                    id=artifact_id,
                    execution_id=command.execution_id,
                    execution_attempt_id=None,
                    execution_step_id=None,
                    execution_step_attempt_id=None,
                    artifact_type=command.artifact_type,
                    storage_type=ArtifactStorageType.PV,
                    status=ArtifactStatus.AVAILABLE,
                    name=name,
                    description=command.description,
                    uri=f"jupyter-pv:///{file.path}",
                    relative_path=file.path,
                    media_type=_media_type(command, file.media_type),
                    size_bytes=file.size_bytes,
                    checksum_sha256=file.checksum_sha256,
                    artifact_metadata={
                        **command.metadata,
                        "materialization": "agent-authored-text",
                        "source_type": command.source_type.value,
                    },
                    identity_hash=identity_hash,
                    created_by_type=command.actor_type,
                    created_by=command.actor_id,
                    updated_by_type=command.actor_type,
                    updated_by=command.actor_id,
                )
                session.add(artifact)
            artifact_id = artifact.id
            session.add(
                CommandReceiptORM(
                    idempotency_key=command.idempotency_key,
                    command_type="execution_artifact_materialize",
                    request_fingerprint=fingerprint,
                    result={"artifact_id": str(artifact_id)},
                )
            )
            if created:
                session.add(
                    OutboxEventORM.from_domain(
                        build_execution_event(
                            execution_id=command.execution_id,
                            event_type="execution.artifact_registered",
                            payload={
                                "execution_attempt_id": None,
                                "execution_step_id": None,
                                "artifact_id": str(artifact_id),
                                "artifact_type": command.artifact_type.value,
                                "storage_type": ArtifactStorageType.PV.value,
                                "status": ArtifactStatus.AVAILABLE.value,
                                "uri": artifact.uri,
                            },
                            actor_type=command.actor_type,
                            actor_id=command.actor_id,
                            traceparent=carrier.traceparent,
                            tracestate=carrier.tracestate,
                        )
                    )
                )
        return artifact_id

    async def _receipt(
        self, idempotency_key: str, fingerprint: str
    ) -> UUID | None:
        async with self._session_factory() as session:
            receipt = await session.scalar(
                select(CommandReceiptORM).where(
                    CommandReceiptORM.idempotency_key == idempotency_key
                )
            )
            return (
                _validate_receipt(receipt, fingerprint)
                if receipt is not None
                else None
            )

    async def _execution(self, execution_id: UUID) -> ExecutionORM:
        async with self._session_factory() as session:
            execution = await session.get(ExecutionORM, execution_id)
            if execution is None:
                raise ExecutionNotFoundError(
                    f"Execution {execution_id} was not found."
                )
            session.expunge(execution)
            return execution

    async def _resolve_content(
        self, command: MaterializeArtifactCommand
    ) -> str:
        if command.source_type == CodeSourceType.INLINE:
            if (
                command.source_content is None
                or command.source_path is not None
            ):
                raise ArtifactRegistrationError(
                    "INLINE Artifact source requires content only."
                )
            content = command.source_content
        else:
            if (
                command.source_path is None
                or command.source_content is not None
            ):
                raise ArtifactRegistrationError(
                    "PATH Artifact source requires path only."
                )
            path = Path(command.source_path)
            if path.is_absolute():
                raise ArtifactRegistrationError(
                    "Artifact input path must be relative."
                )
            resolved = (self._input_root / path).resolve()
            try:
                resolved.relative_to(self._input_root)
            except ValueError as exc:
                raise ArtifactRegistrationError(
                    "Artifact input path escapes the input root."
                ) from exc
            if not resolved.is_file():
                raise ArtifactRegistrationError(
                    "Artifact input file was not found."
                )
            raw = await asyncio.to_thread(resolved.read_bytes)
            if command.source_sha256 is not None and not _same_hash(
                raw, command.source_sha256
            ):
                raise ArtifactRegistrationError(
                    "Artifact input SHA-256 does not match."
                )
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ArtifactRegistrationError(
                    "Artifact input must be UTF-8 text."
                ) from exc
        if not content.strip():
            raise ArtifactRegistrationError(
                "Artifact content must not be blank."
            )
        if len(content.encode()) > self._max_bytes:
            raise ArtifactRegistrationError(
                "Artifact content exceeds the configured size limit."
            )
        return content

    async def _append_notebook(
        self, execution: ExecutionORM, idempotency_key: str, content: str
    ) -> None:
        if execution.notebook_path is None:
            raise ArtifactRegistrationError(
                "Execution notebook is not available."
            )
        notebook = await self._runtime_storage.read_notebook(
            execution.runtime_type,
            execution.runtime_target_id,
            execution.notebook_path,
        )
        document = nbformat.from_dict(notebook)
        if not any(
            cell.get("metadata", {}).get("executor", {}).get("idempotency_key")
            == idempotency_key
            for cell in document.cells
        ):
            document.cells.append(
                nbformat.v4.new_markdown_cell(
                    source=content,
                    metadata={
                        "executor": {"idempotency_key": idempotency_key}
                    },
                )
            )
            await self._runtime_storage.write_notebook(
                execution.runtime_type,
                execution.runtime_target_id,
                execution.notebook_path,
                json.loads(nbformat.writes(document)),
            )


def _fingerprint(command: MaterializeArtifactCommand) -> str:
    payload = asdict(command)
    payload.pop("idempotency_key")
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
    ).hexdigest()


def _validate_receipt(receipt: CommandReceiptORM, fingerprint: str) -> UUID:
    if (
        receipt.command_type != "execution_artifact_materialize"
        or receipt.request_fingerprint != fingerprint
    ):
        raise IdempotencyConflictError(
            "idempotency_key was already used with a different command."
        )
    value = receipt.result.get("artifact_id")
    if not isinstance(value, str):
        raise ArtifactRegistrationError("Artifact receipt is invalid.")
    return UUID(value)


def _artifact_name(command: MaterializeArtifactCommand) -> str:
    default_name = (
        "final-report.md"
        if command.artifact_type == ArtifactType.REPORT
        else "artifact.txt"
    )
    name = command.name or default_name
    if not SAFE_NAME.fullmatch(name):
        raise ArtifactRegistrationError(
            "Artifact name contains unsafe characters."
        )
    if (
        command.artifact_type == ArtifactType.REPORT
        and not name.lower().endswith(".md")
    ):
        name += ".md"
    return name


def _target_path(
    workspace: str, artifact_type: ArtifactType, name: str
) -> str:
    root = PurePosixPath(workspace)
    if artifact_type == ArtifactType.REPORT:
        return (root / "reports" / name).as_posix()
    directory = ARTIFACT_DIRECTORIES.get(artifact_type, "other")
    return (root / "artifacts" / directory / name).as_posix()


def _media_type(
    command: MaterializeArtifactCommand, runtime_media_type: str | None
) -> str:
    if command.media_type is not None:
        return command.media_type
    if command.artifact_type == ArtifactType.REPORT:
        return "text/markdown"
    return runtime_media_type or "text/plain"


def _same_hash(raw: bytes, expected: str) -> bool:
    return hashlib.sha256(raw).hexdigest() == expected.lower()
