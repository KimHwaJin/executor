"""SQLAlchemy read adapter for execution attempts, Step history, and events."""

from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from executor_service.application.execution_queries import (
    ExecutionArtifactView,
    ExecutionAttemptView,
    ExecutionEventView,
    ExecutionStepAttemptView,
    ExecutionTraceView,
)
from executor_service.application.pagination import (
    Page,
    decode_integer_cursor,
    decode_time_cursor,
    encode_integer_cursor,
    encode_time_cursor,
)
from executor_service.domain.enums import ExecutionStatus
from executor_service.domain.errors import (
    ExecutionArtifactNotFoundError,
    ExecutionNotFoundError,
)
from executor_service.domain.models import Execution, ExecutionStep
from executor_service.infrastructure.db.models import (
    ExecutionArtifactORM,
    ExecutionAttemptORM,
    ExecutionORM,
    ExecutionStepAttemptORM,
    ExecutionStepORM,
    OutboxEventORM,
)


class SQLAlchemyExecutionQueryService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def executions(
        self,
        *,
        requested_by_user_id: str | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        status: ExecutionStatus | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[Execution]:
        statement = select(ExecutionORM).options(selectinload(ExecutionORM.steps))
        if requested_by_user_id is not None:
            statement = statement.where(ExecutionORM.requested_by_user_id == requested_by_user_id)
        if project_id is not None:
            statement = statement.where(ExecutionORM.project_id == project_id)
        if session_id is not None:
            statement = statement.where(ExecutionORM.session_id == session_id)
        if task_id is not None:
            statement = statement.where(ExecutionORM.task_id == task_id)
        if status is not None:
            statement = statement.where(ExecutionORM.status == status)
        if cursor is not None:
            created_at, item_id = decode_time_cursor(cursor, "executions")
            statement = statement.where(
                or_(
                    ExecutionORM.created_at < created_at,
                    and_(
                        ExecutionORM.created_at == created_at,
                        ExecutionORM.id < item_id,
                    ),
                )
            )
        statement = statement.order_by(
            ExecutionORM.created_at.desc(), ExecutionORM.id.desc()
        ).limit(limit + 1)
        async with self._session_factory() as session:
            rows = list(await session.scalars(statement))
        page_rows = rows[:limit]
        next_cursor = (
            encode_time_cursor("executions", page_rows[-1].created_at, page_rows[-1].id)
            if len(rows) > limit and page_rows
            else None
        )
        return Page(items=[row.to_domain() for row in page_rows], next_cursor=next_cursor)

    async def steps(
        self, execution_id: UUID, *, cursor: str | None = None, limit: int = 100
    ) -> Page[ExecutionStep]:
        async with self._session_factory() as session:
            await self._require_execution(session, execution_id)
            statement = select(ExecutionStepORM).where(
                ExecutionStepORM.execution_id == execution_id
            )
            if cursor is not None:
                sequence = decode_integer_cursor(cursor, "execution_steps")
                statement = statement.where(ExecutionStepORM.sequence > sequence)
            rows = list(
                await session.scalars(
                    statement.order_by(ExecutionStepORM.sequence).limit(limit + 1)
                )
            )
        page_rows = rows[:limit]
        next_cursor = (
            encode_integer_cursor("execution_steps", page_rows[-1].sequence)
            if len(rows) > limit and page_rows
            else None
        )
        return Page(items=[row.to_domain() for row in page_rows], next_cursor=next_cursor)

    async def attempts(
        self, execution_id: UUID, *, cursor: str | None = None, limit: int = 100
    ) -> Page[ExecutionAttemptView]:
        async with self._session_factory() as session:
            await self._require_execution(session, execution_id)
            statement = select(ExecutionAttemptORM).where(
                ExecutionAttemptORM.execution_id == execution_id
            )
            if cursor is not None:
                attempt_number = decode_integer_cursor(cursor, "execution_attempts")
                statement = statement.where(ExecutionAttemptORM.attempt_number > attempt_number)
            attempts = list(
                await session.scalars(
                    statement.order_by(ExecutionAttemptORM.attempt_number).limit(limit + 1)
                )
            )
            page_attempts = attempts[:limit]
            attempt_ids = [attempt.id for attempt in page_attempts]
            step_rows = (
                list(
                    await session.scalars(
                        select(ExecutionStepAttemptORM)
                        .where(ExecutionStepAttemptORM.execution_attempt_id.in_(attempt_ids))
                        .order_by(
                            ExecutionStepAttemptORM.execution_attempt_id,
                            ExecutionStepAttemptORM.sequence,
                        )
                    )
                )
                if attempt_ids
                else []
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
                    created_by_type=row.created_by_type,
                    created_by=row.created_by,
                    updated_by_type=row.updated_by_type,
                    updated_by=row.updated_by,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                    started_at=row.started_at,
                    finished_at=row.finished_at,
                )
            )
        items = [
            ExecutionAttemptView(
                id=row.id,
                execution_id=row.execution_id,
                attempt_number=row.attempt_number,
                runtime_target_id=row.runtime_target_id,
                runtime_session_id=row.runtime_session_id,
                status=row.status,
                lease_owner=row.lease_owner,
                lease_expires_at=row.lease_expires_at,
                heartbeat_at=row.heartbeat_at,
                error_message=row.error_message,
                failure_type=row.failure_type,
                retry_strategy=row.retry_strategy,
                runtime_session_cleanup_status=row.runtime_session_cleanup_status,
                created_by_type=row.created_by_type,
                created_by=row.created_by,
                updated_by_type=row.updated_by_type,
                updated_by=row.updated_by,
                created_at=row.created_at,
                updated_at=row.updated_at,
                started_at=row.started_at,
                finished_at=row.finished_at,
                steps=tuple(steps_by_attempt.get(row.id, [])),
            )
            for row in page_attempts
        ]
        next_cursor = (
            encode_integer_cursor("execution_attempts", page_attempts[-1].attempt_number)
            if len(attempts) > limit and page_attempts
            else None
        )
        return Page(items=items, next_cursor=next_cursor)

    async def events(
        self, execution_id: UUID, *, cursor: str | None = None, limit: int = 200
    ) -> Page[ExecutionEventView]:
        async with self._session_factory() as session:
            await self._require_execution(session, execution_id)
            statement = select(OutboxEventORM).where(
                OutboxEventORM.aggregate_type == "Execution",
                OutboxEventORM.aggregate_id == execution_id,
            )
            if cursor is not None:
                created_at, item_id = decode_time_cursor(cursor, "execution_events")
                statement = statement.where(
                    or_(
                        OutboxEventORM.created_at > created_at,
                        and_(
                            OutboxEventORM.created_at == created_at,
                            OutboxEventORM.id > item_id,
                        ),
                    )
                )
            rows = list(
                await session.scalars(
                    statement.order_by(OutboxEventORM.created_at, OutboxEventORM.id).limit(
                        limit + 1
                    )
                )
            )
        page_rows = rows[:limit]
        items = [
            ExecutionEventView(
                id=row.id,
                event_type=row.event_type,
                payload=_redact(row.payload),
                delivery_status=row.status,
                publish_attempt_count=row.attempt_count,
                created_by_type=row.created_by_type,
                created_by=row.created_by,
                updated_by_type=row.updated_by_type,
                updated_by=row.updated_by,
                available_at=row.available_at,
                created_at=row.created_at,
                updated_at=row.updated_at,
                published_at=row.published_at,
                last_error=row.last_error,
            )
            for row in page_rows
        ]
        next_cursor = (
            encode_time_cursor("execution_events", page_rows[-1].created_at, page_rows[-1].id)
            if len(rows) > limit and page_rows
            else None
        )
        return Page(items=items, next_cursor=next_cursor)

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
            attempts=await self.attempts(execution_id),
            events=await self.events(execution_id),
            artifacts=await self.artifacts(execution_id),
        )

    async def artifacts(
        self, execution_id: UUID, *, cursor: str | None = None, limit: int = 500
    ) -> Page[ExecutionArtifactView]:
        async with self._session_factory() as session:
            await self._require_execution(session, execution_id)
            statement = select(ExecutionArtifactORM).where(
                ExecutionArtifactORM.execution_id == execution_id
            )
            if cursor is not None:
                created_at, item_id = decode_time_cursor(cursor, "execution_artifacts")
                statement = statement.where(
                    or_(
                        ExecutionArtifactORM.created_at > created_at,
                        and_(
                            ExecutionArtifactORM.created_at == created_at,
                            ExecutionArtifactORM.id > item_id,
                        ),
                    )
                )
            rows = list(
                await session.scalars(
                    statement.order_by(
                        ExecutionArtifactORM.created_at, ExecutionArtifactORM.id
                    ).limit(limit + 1)
                )
            )
        page_rows = rows[:limit]
        next_cursor = (
            encode_time_cursor("execution_artifacts", page_rows[-1].created_at, page_rows[-1].id)
            if len(rows) > limit and page_rows
            else None
        )
        return Page(
            items=[_artifact_view(row) for row in page_rows],
            next_cursor=next_cursor,
        )

    async def artifact(self, artifact_id: UUID) -> ExecutionArtifactView:
        async with self._session_factory() as session:
            row = await session.get(ExecutionArtifactORM, artifact_id)
        if row is None:
            raise ExecutionArtifactNotFoundError(f"Execution Artifact {artifact_id} was not found.")
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
        created_by_type=row.created_by_type,
        created_by=row.created_by,
        updated_by_type=row.updated_by_type,
        updated_by=row.updated_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
