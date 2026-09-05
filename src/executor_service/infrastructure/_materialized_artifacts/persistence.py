"""Persistence and idempotency receipts for materialized Artifacts."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.application.commands import MaterializeArtifactCommand
from executor_service.domain.enums import ArtifactStatus, ArtifactStorageType
from executor_service.domain.errors import (
    ArtifactRegistrationError,
    ExecutionNotFoundError,
    IdempotencyConflictError,
)
from executor_service.domain.runtime import RuntimeFileMetadata
from executor_service.infrastructure.db.models import (
    CommandReceiptORM,
    ExecutionArtifactORM,
    ExecutionORM,
)


class MaterializedArtifactPersistence:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        self._session_factory = session_factory

    async def receipt(
        self, idempotency_key: str, fingerprint: str
    ) -> UUID | None:
        async with self._session_factory() as session:
            receipt = await session.scalar(
                select(CommandReceiptORM).where(
                    CommandReceiptORM.idempotency_key == idempotency_key
                )
            )
            return (
                validate_receipt(receipt, fingerprint)
                if receipt is not None
                else None
            )

    async def execution(self, execution_id: UUID) -> ExecutionORM:
        async with self._session_factory() as session:
            execution = await session.get(ExecutionORM, execution_id)
            if execution is None:
                raise ExecutionNotFoundError(
                    f"Execution {execution_id} was not found."
                )
            session.expunge(execution)
            return execution

    async def reserve(
        self, command: MaterializeArtifactCommand, fingerprint: str
    ) -> None:
        """Commit key ownership before any externally visible file change.

        A failed materialization keeps its receipt pending. Only the identical
        command may resume it; a conflicting request must never touch storage.
        """
        try:
            async with self._session_factory() as session, session.begin():
                session.add(
                    CommandReceiptORM(
                        idempotency_key=command.idempotency_key,
                        command_type="execution_artifact_materialize",
                        request_fingerprint=fingerprint,
                        result={"materialization_status": "PENDING"},
                    )
                )
        except IntegrityError:
            # The unique key also arbitrates between different Executions
            # and other command types, across independent API processes.
            async with self._session_factory() as session:
                receipt = await session.scalar(
                    select(CommandReceiptORM).where(
                        CommandReceiptORM.idempotency_key
                        == command.idempotency_key
                    )
                )
                if receipt is None:
                    raise
                validate_receipt(receipt, fingerprint)

    @asynccontextmanager
    async def locked(
        self, execution_id: UUID
    ) -> AsyncIterator[tuple[AsyncSession, ExecutionORM]]:
        """Serialize file and notebook changes across Executor instances."""
        async with self._session_factory() as session, session.begin():
            if session.get_bind().dialect.name == "postgresql":
                await session.execute(
                    text("SELECT set_config('lock_timeout', '30s', true)")
                )
            execution = await session.scalar(
                select(ExecutionORM)
                .where(ExecutionORM.id == execution_id)
                .with_for_update()
            )
            if execution is None:
                raise ExecutionNotFoundError(
                    f"Execution {execution_id} was not found."
                )
            yield session, execution

    async def completed(
        self, session: AsyncSession, key: str, fingerprint: str
    ) -> UUID | None:
        receipt = await session.scalar(
            select(CommandReceiptORM).where(
                CommandReceiptORM.idempotency_key == key
            )
        )
        if receipt is None:
            raise ArtifactRegistrationError("Artifact reservation is missing.")
        return validate_receipt(receipt, fingerprint)

    async def persist(
        self,
        command: MaterializeArtifactCommand,
        *,
        session: AsyncSession,
        fingerprint: str,
        artifact_id: UUID,
        identity_hash: str,
        name: str,
        media_type: str,
        file: RuntimeFileMetadata,
    ) -> UUID:
        async with session.begin_nested():
            repeated = await session.scalar(
                select(CommandReceiptORM).where(
                    CommandReceiptORM.idempotency_key
                    == command.idempotency_key
                )
            )
            if repeated is None:
                raise ArtifactRegistrationError(
                    "Artifact command must be reserved before writing."
                )
            completed = validate_receipt(repeated, fingerprint)
            if completed is not None:
                return completed
            artifact = await session.scalar(
                select(ExecutionArtifactORM).where(
                    ExecutionArtifactORM.identity_hash == identity_hash
                )
            )
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
                    media_type=media_type,
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
            repeated.result = {
                "artifact_id": str(artifact_id),
                "materialization_status": "COMPLETED",
            }
        return artifact_id


def validate_receipt(
    receipt: CommandReceiptORM, fingerprint: str
) -> UUID | None:
    if (
        receipt.command_type != "execution_artifact_materialize"
        or receipt.request_fingerprint != fingerprint
    ):
        raise IdempotencyConflictError(
            "idempotency_key was already used with a different command."
        )
    if receipt.result.get("materialization_status") == "PENDING":
        return None
    value = receipt.result.get("artifact_id")
    if not isinstance(value, str):
        raise ArtifactRegistrationError("Artifact receipt is invalid.")
    return UUID(value)
