"""SQLAlchemy read adapter for execution attempts, Step history, and events."""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from executor_service.application.execution_queries import (
    ExecutionArtifactView,
    ExecutionAttemptView,
    ExecutionEventView,
    ExecutionStepAttemptView,
    ExecutionTraceView,
)
from executor_service.domain.errors import (
    ExecutionArtifactNotFoundError,
    ExecutionNotFoundError,
)
from executor_service.infrastructure.db.models import (
    ExecutionArtifactORM,
    ExecutionAttemptORM,
    ExecutionORM,
    ExecutionStepAttemptORM,
    OutboxEventORM,
)


class SQLAlchemyExecutionQueryService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def attempts(
        self, execution_id: UUID, *, limit: int = 100
    ) -> list[ExecutionAttemptView]:
        async with self._session_factory() as session:
            await self._require_execution(session, execution_id)
            attempts = list(
                await session.scalars(
                    select(ExecutionAttemptORM)
                    .where(ExecutionAttemptORM.execution_id == execution_id)
                    .order_by(ExecutionAttemptORM.attempt_number)
                    .limit(limit)
                )
            )
            step_rows = list(
                await session.scalars(
                    select(ExecutionStepAttemptORM)
                    .where(ExecutionStepAttemptORM.execution_id == execution_id)
                    .order_by(
                        ExecutionStepAttemptORM.execution_attempt_id,
                        ExecutionStepAttemptORM.sequence,
                    )
                )
            )
        steps_by_attempt: dict[UUID, list[ExecutionStepAttemptView]] = {}
        for row in step_rows:
            steps_by_attempt.setdefault(row.execution_attempt_id, []).append(
                ExecutionStepAttemptView(
                    id=row.id,
                    execution_attempt_id=row.execution_attempt_id,
                    execution_step_id=row.execution_step_id,
                    sequence=row.sequence,
                    skill_name=row.skill_name,
                    tool_name=row.tool_name,
                    input_parameters=_redact(row.input_parameters),
                    status=row.status,
                    outputs=_redact(row.outputs),
                    error_message=row.error_message,
                    started_at=row.started_at,
                    finished_at=row.finished_at,
                )
            )
        return [
            ExecutionAttemptView(
                id=row.id,
                execution_id=row.execution_id,
                attempt_number=row.attempt_number,
                jupyter_server_id=row.jupyter_server_id,
                kernel_id=row.kernel_id,
                status=row.status,
                lease_owner=row.lease_owner,
                lease_expires_at=row.lease_expires_at,
                heartbeat_at=row.heartbeat_at,
                error_message=row.error_message,
                failure_type=row.failure_type,
                retry_strategy=row.retry_strategy,
                kernel_cleanup_status=row.kernel_cleanup_status,
                started_at=row.started_at,
                finished_at=row.finished_at,
                steps=tuple(steps_by_attempt.get(row.id, [])),
            )
            for row in attempts
        ]

    async def events(
        self, execution_id: UUID, *, limit: int = 200
    ) -> list[ExecutionEventView]:
        async with self._session_factory() as session:
            await self._require_execution(session, execution_id)
            rows = list(
                await session.scalars(
                    select(OutboxEventORM)
                    .where(
                        OutboxEventORM.aggregate_type == "Execution",
                        OutboxEventORM.aggregate_id == execution_id,
                    )
                    .order_by(OutboxEventORM.created_at, OutboxEventORM.id)
                    .limit(limit)
                )
            )
        return [
            ExecutionEventView(
                id=row.id,
                event_type=row.event_type,
                payload=_redact(row.payload),
                delivery_status=row.status,
                publish_attempt_count=row.attempt_count,
                available_at=row.available_at,
                created_at=row.created_at,
                published_at=row.published_at,
                last_error=row.last_error,
            )
            for row in rows
        ]

    async def trace(self, execution_id: UUID) -> ExecutionTraceView:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(ExecutionORM)
                .where(ExecutionORM.id == execution_id)
                .options(selectinload(ExecutionORM.steps))
            )
            if row is None:
                raise ExecutionNotFoundError(f"Execution {execution_id} was not found.")
            execution = row.to_domain()
        return ExecutionTraceView(
            execution=execution,
            attempts=tuple(await self.attempts(execution_id)),
            events=tuple(await self.events(execution_id)),
            artifacts=tuple(await self.artifacts(execution_id)),
        )

    async def artifacts(
        self, execution_id: UUID, *, limit: int = 500
    ) -> list[ExecutionArtifactView]:
        async with self._session_factory() as session:
            await self._require_execution(session, execution_id)
            rows = list(
                await session.scalars(
                    select(ExecutionArtifactORM)
                    .where(ExecutionArtifactORM.execution_id == execution_id)
                    .order_by(ExecutionArtifactORM.created_at, ExecutionArtifactORM.id)
                    .limit(limit)
                )
            )
        return [_artifact_view(row) for row in rows]

    async def artifact(self, artifact_id: UUID) -> ExecutionArtifactView:
        async with self._session_factory() as session:
            row = await session.get(ExecutionArtifactORM, artifact_id)
        if row is None:
            raise ExecutionArtifactNotFoundError(
                f"Execution Artifact {artifact_id} was not found."
            )
        return _artifact_view(row)

    @staticmethod
    async def _require_execution(session: AsyncSession, execution_id: UUID) -> None:
        exists = await session.scalar(
            select(ExecutionORM.id).where(ExecutionORM.id == execution_id)
        )
        if exists is None:
            raise ExecutionNotFoundError(f"Execution {execution_id} was not found.")


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


def _artifact_view(row: ExecutionArtifactORM) -> ExecutionArtifactView:
    return ExecutionArtifactView(
        id=row.id,
        execution_id=row.execution_id,
        execution_attempt_id=row.execution_attempt_id,
        execution_step_id=row.execution_step_id,
        execution_step_attempt_id=row.execution_step_attempt_id,
        parent_artifact_id=row.parent_artifact_id,
        external_parent_asset_id=row.external_parent_asset_id,
        artifact_type=row.artifact_type,
        storage_type=row.storage_type,
        status=row.status,
        name=row.name,
        description=row.description,
        uri=row.uri,
        relative_path=row.relative_path,
        media_type=row.media_type,
        size_bytes=row.size_bytes,
        checksum_sha256=row.checksum_sha256,
        metadata=_redact(row.artifact_metadata),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
