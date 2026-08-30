"""Materialize Agent-authored text as a Runtime-owned Execution Artifact."""

from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.application.commands import MaterializeArtifactCommand
from executor_service.domain.enums import ExecutionStatus
from executor_service.domain.errors import (
    ArtifactRegistrationError,
    InvalidStateTransitionError,
)
from executor_service.domain.runtime import RuntimeStorageAccess
from executor_service.infrastructure._materialized_artifacts import (
    ArtifactContentResolver,
    MaterializedArtifactPersistence,
    append_notebook_markdown,
    artifact_id,
    artifact_identity_hash,
    artifact_name,
    command_fingerprint,
    media_type,
    target_path,
)


class MaterializedArtifactService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        runtime_storage: RuntimeStorageAccess,
        input_root: Path,
        *,
        max_bytes: int,
    ) -> None:
        self._runtime_storage = runtime_storage
        self._content = ArtifactContentResolver(
            input_root,
            max_bytes=max_bytes,
        )
        self._persistence = MaterializedArtifactPersistence(session_factory)

    async def materialize(self, command: MaterializeArtifactCommand) -> UUID:
        fingerprint = command_fingerprint(command)
        existing = await self._persistence.receipt(
            command.idempotency_key, fingerprint
        )
        if existing is not None:
            return existing
        content = await self._content.resolve(command)
        execution = await self._persistence.execution(command.execution_id)
        if execution.status != ExecutionStatus.SUCCEEDED:
            raise InvalidStateTransitionError(
                "Execution Artifacts authored after execution require "
                "SUCCEEDED state."
            )
        if execution.workspace_path is None:
            raise ArtifactRegistrationError(
                "Execution workspace is not available."
            )
        name = artifact_name(command)
        destination = target_path(
            execution.workspace_path, command.artifact_type, name
        )
        file = await self._runtime_storage.write_text(
            execution.runtime_type,
            execution.runtime_target_id,
            destination,
            content,
        )
        if command.append_to_notebook:
            await append_notebook_markdown(
                self._runtime_storage,
                execution,
                command.idempotency_key,
                content,
            )

        return await self._persistence.persist(
            command,
            fingerprint=fingerprint,
            artifact_id=artifact_id(fingerprint),
            identity_hash=artifact_identity_hash(
                command.execution_id,
                destination,
                file.checksum_sha256,
            ),
            name=name,
            media_type=media_type(command, file.media_type),
            file=file,
        )
