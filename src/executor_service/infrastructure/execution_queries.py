"""SQLAlchemy read adapter for execution attempts, Step history, and events."""

from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import load_only, noload

from executor_service.application.execution_queries import (
    ExecutionArtifactView,
    ExecutionAttemptView,
    ExecutionDetailView,
    ExecutionEventView,
    ExecutionOperationView,
    ExecutionStepAttemptView,
    ExecutionSummaryView,
)
from executor_service.application.pagination import (
    Page,
    decode_integer_cursor,
    decode_time_cursor,
    encode_integer_cursor,
    encode_time_cursor,
)
from executor_service.domain.enums import ExecutionStatus, OutboxDestination
from executor_service.domain.errors import (
    ExecutionArtifactNotFoundError,
    ExecutionAttemptNotFoundError,
    ExecutionNotFoundError,
    ExecutionOperationNotFoundError,
)
from executor_service.domain.models import ExecutionStep
from executor_service.infrastructure.db.models import (
    ExecutionArtifactORM,
    ExecutionAttemptORM,
    ExecutionOperationORM,
    ExecutionORM,
    ExecutionStepAttemptORM,
    ExecutionStepORM,
    OutboxEventORM,
)

_EXECUTION_SUMMARY_COLUMNS = (
    ExecutionORM.id,
    ExecutionORM.operation_mode,
    ExecutionORM.operation_wait_timeout_seconds,
    ExecutionORM.trigger_type,
    ExecutionORM.user_id,
    ExecutionORM.project_id,
    ExecutionORM.session_id,
    ExecutionORM.task_id,
    ExecutionORM.workflow_id,
    ExecutionORM.status,
    ExecutionORM.version,
    ExecutionORM.created_by_type,
    ExecutionORM.created_by,
    ExecutionORM.updated_by_type,
    ExecutionORM.updated_by,
    ExecutionORM.created_at,
    ExecutionORM.updated_at,
    ExecutionORM.started_at,
    ExecutionORM.finished_at,
)

_EXECUTION_DETAIL_COLUMNS = (
    *_EXECUTION_SUMMARY_COLUMNS,
    ExecutionORM.runtime_type,
    ExecutionORM.runtime_pool,
    ExecutionORM.runtime_profile,
    ExecutionORM.runtime_target_id,
    ExecutionORM.runtime_session_id,
    ExecutionORM.cancellation_reason,
    ExecutionORM.workspace_path,
    ExecutionORM.notebook_path,
    ExecutionORM.failure_type,
    ExecutionORM.error_message,
    ExecutionORM.retry_strategy,
    ExecutionORM.retry_count,
    ExecutionORM.retry_from_sequence,
    ExecutionORM.retained_runtime_session_until,
    ExecutionORM.recovery_count,
    ExecutionORM.runtime_session_cleanup_status,
    ExecutionORM.operation_wait_expires_at,
    ExecutionORM.execution_expires_at,
)


class SQLAlchemyExecutionQueryService:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        self._session_factory = session_factory

    async def executions(
        self,
        *,
        user_id: str | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        status: ExecutionStatus | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[ExecutionSummaryView]:
        step_count = (
            select(func.count(ExecutionStepORM.id))
            .where(ExecutionStepORM.execution_id == ExecutionORM.id)
            .correlate(ExecutionORM)
            .scalar_subquery()
        )
        statement = select(
            ExecutionORM, step_count.label("step_count")
        ).options(
            load_only(*_EXECUTION_SUMMARY_COLUMNS),
            noload(ExecutionORM.steps),
        )
        if user_id is not None:
            statement = statement.where(ExecutionORM.user_id == user_id)
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
            rows = list((await session.execute(statement)).all())
        page_rows = rows[:limit]
        next_cursor = (
            encode_time_cursor(
                "executions", page_rows[-1][0].created_at, page_rows[-1][0].id
            )
            if len(rows) > limit and page_rows
            else None
        )
        return Page(
            items=[
                _execution_summary_view(row, count) for row, count in page_rows
            ],
            next_cursor=next_cursor,
        )

    async def execution(self, execution_id: UUID) -> ExecutionDetailView:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(ExecutionORM)
                .where(ExecutionORM.id == execution_id)
                .options(
                    load_only(*_EXECUTION_DETAIL_COLUMNS),
                    noload(ExecutionORM.steps),
                )
            )
        if row is None:
            raise ExecutionNotFoundError(
                f"Execution {execution_id} was not found."
            )
        return _execution_detail_view(row)

    async def steps(
        self,
        execution_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[ExecutionStep]:
        async with self._session_factory() as session:
            await self._require_execution(session, execution_id)
            statement = select(ExecutionStepORM).where(
                ExecutionStepORM.execution_id == execution_id
            )
            if cursor is not None:
                sequence = decode_integer_cursor(cursor, "execution_steps")
                statement = statement.where(
                    ExecutionStepORM.sequence > sequence
                )
            rows = list(
                await session.scalars(
                    statement.order_by(ExecutionStepORM.sequence).limit(
                        limit + 1
                    )
                )
            )
        page_rows = rows[:limit]
        next_cursor = (
            encode_integer_cursor("execution_steps", page_rows[-1].sequence)
            if len(rows) > limit and page_rows
            else None
        )
        return Page(
            items=[row.to_domain() for row in page_rows],
            next_cursor=next_cursor,
        )

    async def attempts(
        self,
        execution_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[ExecutionAttemptView]:
        async with self._session_factory() as session:
            await self._require_execution(session, execution_id)
            statement = select(ExecutionAttemptORM).where(
                ExecutionAttemptORM.execution_id == execution_id
            )
            if cursor is not None:
                attempt_number = decode_integer_cursor(
                    cursor, "execution_attempts"
                )
                statement = statement.where(
                    ExecutionAttemptORM.attempt_number > attempt_number
                )
            attempts = list(
                await session.scalars(
                    statement.order_by(
                        ExecutionAttemptORM.attempt_number
                    ).limit(limit + 1)
                )
            )
            page_attempts = attempts[:limit]
            step_counts = await self._step_counts(
                session, [row.id for row in page_attempts]
            )
        items = [
            _attempt_view(row, step_counts.get(row.id, 0))
            for row in page_attempts
        ]
        next_cursor = (
            encode_integer_cursor(
                "execution_attempts", page_attempts[-1].attempt_number
            )
            if len(attempts) > limit and page_attempts
            else None
        )
        return Page(items=items, next_cursor=next_cursor)

    async def attempt(
        self, execution_id: UUID, attempt_id: UUID
    ) -> ExecutionAttemptView:
        async with self._session_factory() as session:
            await self._require_execution(session, execution_id)
            row = await session.scalar(
                select(ExecutionAttemptORM).where(
                    ExecutionAttemptORM.id == attempt_id,
                    ExecutionAttemptORM.execution_id == execution_id,
                )
            )
            if row is None:
                raise ExecutionAttemptNotFoundError(
                    f"Execution Attempt {attempt_id} was not found in Execution {execution_id}."
                )
            step_count = await session.scalar(
                select(func.count(ExecutionStepAttemptORM.id)).where(
                    ExecutionStepAttemptORM.execution_attempt_id == attempt_id
                )
            )
        return _attempt_view(row, step_count or 0)

    async def operations(
        self,
        execution_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[ExecutionOperationView]:
        async with self._session_factory() as session:
            await self._require_execution(session, execution_id)
            statement = select(ExecutionOperationORM).where(
                ExecutionOperationORM.execution_id == execution_id
            )
            if cursor is not None:
                operation_number = decode_integer_cursor(
                    cursor, "execution_operations"
                )
                statement = statement.where(
                    ExecutionOperationORM.operation_number > operation_number
                )
            rows = list(
                await session.scalars(
                    statement.order_by(
                        ExecutionOperationORM.operation_number
                    ).limit(limit + 1)
                )
            )
        page_rows = rows[:limit]
        next_cursor = (
            encode_integer_cursor(
                "execution_operations", page_rows[-1].operation_number
            )
            if len(rows) > limit and page_rows
            else None
        )
        return Page(
            items=[_operation_view(row) for row in page_rows],
            next_cursor=next_cursor,
        )

    async def operation(
        self, execution_id: UUID, operation_id: UUID
    ) -> ExecutionOperationView:
        async with self._session_factory() as session:
            await self._require_execution(session, execution_id)
            row = await session.scalar(
                select(ExecutionOperationORM).where(
                    ExecutionOperationORM.id == operation_id,
                    ExecutionOperationORM.execution_id == execution_id,
                )
            )
        if row is None:
            raise ExecutionOperationNotFoundError(
                f"Execution Operation {operation_id} was not found in Execution {execution_id}."
            )
        return _operation_view(row)

    async def operation_steps(
        self,
        execution_id: UUID,
        operation_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[ExecutionStep]:
        async with self._session_factory() as session:
            await self._require_operation(session, execution_id, operation_id)
            statement = select(ExecutionStepORM).where(
                ExecutionStepORM.execution_id == execution_id,
                ExecutionStepORM.operation_id == operation_id,
            )
            if cursor is not None:
                sequence = decode_integer_cursor(
                    cursor, "execution_operation_steps"
                )
                statement = statement.where(
                    ExecutionStepORM.sequence > sequence
                )
            rows = list(
                await session.scalars(
                    statement.order_by(ExecutionStepORM.sequence).limit(
                        limit + 1
                    )
                )
            )
        page_rows = rows[:limit]
        next_cursor = (
            encode_integer_cursor(
                "execution_operation_steps", page_rows[-1].sequence
            )
            if len(rows) > limit and page_rows
            else None
        )
        return Page(
            items=[row.to_domain() for row in page_rows],
            next_cursor=next_cursor,
        )

    async def attempt_steps(
        self,
        execution_id: UUID,
        attempt_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[ExecutionStepAttemptView]:
        async with self._session_factory() as session:
            await self._require_attempt(session, execution_id, attempt_id)
            statement = select(ExecutionStepAttemptORM).where(
                ExecutionStepAttemptORM.execution_attempt_id == attempt_id
            )
            if cursor is not None:
                sequence = decode_integer_cursor(
                    cursor, "execution_step_attempts"
                )
                statement = statement.where(
                    ExecutionStepAttemptORM.sequence > sequence
                )
            rows = list(
                await session.scalars(
                    statement.order_by(ExecutionStepAttemptORM.sequence).limit(
                        limit + 1
                    )
                )
            )
        page_rows = rows[:limit]
        next_cursor = (
            encode_integer_cursor(
                "execution_step_attempts", page_rows[-1].sequence
            )
            if len(rows) > limit and page_rows
            else None
        )
        return Page(
            items=[_step_attempt_view(row) for row in page_rows],
            next_cursor=next_cursor,
        )

    async def events(
        self,
        execution_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 200,
    ) -> Page[ExecutionEventView]:
        async with self._session_factory() as session:
            await self._require_execution(session, execution_id)
            statement = select(OutboxEventORM).where(
                OutboxEventORM.aggregate_type == "Execution",
                OutboxEventORM.aggregate_id == execution_id,
                OutboxEventORM.destination == OutboxDestination.EVENTS,
            )
            if cursor is not None:
                created_at, item_id = decode_time_cursor(
                    cursor, "execution_events"
                )
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
                    statement.order_by(
                        OutboxEventORM.created_at, OutboxEventORM.id
                    ).limit(limit + 1)
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
            encode_time_cursor(
                "execution_events", page_rows[-1].created_at, page_rows[-1].id
            )
            if len(rows) > limit and page_rows
            else None
        )
        return Page(items=items, next_cursor=next_cursor)

    async def artifacts(
        self,
        execution_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 500,
    ) -> Page[ExecutionArtifactView]:
        async with self._session_factory() as session:
            await self._require_execution(session, execution_id)
            statement = select(ExecutionArtifactORM).where(
                ExecutionArtifactORM.execution_id == execution_id
            )
            if cursor is not None:
                created_at, item_id = decode_time_cursor(
                    cursor, "execution_artifacts"
                )
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
                        ExecutionArtifactORM.created_at,
                        ExecutionArtifactORM.id,
                    ).limit(limit + 1)
                )
            )
        page_rows = rows[:limit]
        next_cursor = (
            encode_time_cursor(
                "execution_artifacts",
                page_rows[-1].created_at,
                page_rows[-1].id,
            )
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
            raise ExecutionArtifactNotFoundError(
                f"Execution Artifact {artifact_id} was not found."
            )
        return _artifact_view(row)

    @staticmethod
    async def _require_execution(
        session: AsyncSession, execution_id: UUID
    ) -> None:
        exists = await session.scalar(
            select(ExecutionORM.id).where(ExecutionORM.id == execution_id)
        )
        if exists is None:
            raise ExecutionNotFoundError(
                f"Execution {execution_id} was not found."
            )

    @staticmethod
    async def _require_attempt(
        session: AsyncSession, execution_id: UUID, attempt_id: UUID
    ) -> None:
        await SQLAlchemyExecutionQueryService._require_execution(
            session, execution_id
        )
        exists = await session.scalar(
            select(ExecutionAttemptORM.id).where(
                ExecutionAttemptORM.id == attempt_id,
                ExecutionAttemptORM.execution_id == execution_id,
            )
        )
        if exists is None:
            raise ExecutionAttemptNotFoundError(
                f"Execution Attempt {attempt_id} was not found in Execution {execution_id}."
            )

    @staticmethod
    async def _require_operation(
        session: AsyncSession, execution_id: UUID, operation_id: UUID
    ) -> None:
        await SQLAlchemyExecutionQueryService._require_execution(
            session, execution_id
        )
        exists = await session.scalar(
            select(ExecutionOperationORM.id).where(
                ExecutionOperationORM.id == operation_id,
                ExecutionOperationORM.execution_id == execution_id,
            )
        )
        if exists is None:
            raise ExecutionOperationNotFoundError(
                f"Execution Operation {operation_id} was not found in Execution {execution_id}."
            )

    @staticmethod
    async def _step_counts(
        session: AsyncSession, attempt_ids: list[UUID]
    ) -> dict[UUID, int]:
        if not attempt_ids:
            return {}
        rows = await session.execute(
            select(
                ExecutionStepAttemptORM.execution_attempt_id,
                func.count(ExecutionStepAttemptORM.id),
            )
            .where(
                ExecutionStepAttemptORM.execution_attempt_id.in_(attempt_ids)
            )
            .group_by(ExecutionStepAttemptORM.execution_attempt_id)
        )
        return {attempt_id: count for attempt_id, count in rows}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _is_secret_key(key) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _execution_summary_view(
    row: ExecutionORM, step_count: int
) -> ExecutionSummaryView:
    return ExecutionSummaryView(
        id=row.id,
        operation_mode=row.operation_mode,
        operation_wait_timeout_seconds=row.operation_wait_timeout_seconds,
        trigger_type=row.trigger_type,
        user_id=row.user_id,
        project_id=row.project_id,
        session_id=row.session_id,
        task_id=row.task_id,
        workflow_id=row.workflow_id,
        status=row.status,
        version=row.version,
        step_count=step_count,
        created_by_type=row.created_by_type,
        created_by=row.created_by,
        updated_by_type=row.updated_by_type,
        updated_by=row.updated_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def _execution_detail_view(row: ExecutionORM) -> ExecutionDetailView:
    return ExecutionDetailView(
        id=row.id,
        operation_mode=row.operation_mode,
        operation_wait_timeout_seconds=row.operation_wait_timeout_seconds,
        trigger_type=row.trigger_type,
        user_id=row.user_id,
        project_id=row.project_id,
        session_id=row.session_id,
        task_id=row.task_id,
        workflow_id=row.workflow_id,
        runtime_type=row.runtime_type,
        runtime_pool=row.runtime_pool,
        runtime_profile=row.runtime_profile,
        runtime_target_id=row.runtime_target_id,
        runtime_session_id=row.runtime_session_id,
        status=row.status,
        version=row.version,
        cancellation_reason=row.cancellation_reason,
        workspace_path=row.workspace_path,
        notebook_path=row.notebook_path,
        failure_type=row.failure_type,
        error_message=row.error_message,
        retry_strategy=row.retry_strategy,
        retry_count=row.retry_count,
        retry_from_sequence=row.retry_from_sequence,
        retained_runtime_session_until=row.retained_runtime_session_until,
        recovery_count=row.recovery_count,
        runtime_session_cleanup_status=row.runtime_session_cleanup_status,
        operation_wait_expires_at=row.operation_wait_expires_at,
        execution_expires_at=row.execution_expires_at,
        created_by_type=row.created_by_type,
        created_by=row.created_by,
        updated_by_type=row.updated_by_type,
        updated_by=row.updated_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def _is_secret_key(key: str) -> bool:
    normalized = key.lower()
    return any(
        marker in normalized
        for marker in ("token", "secret", "password", "credential")
    )


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


def _attempt_view(
    row: ExecutionAttemptORM, step_count: int
) -> ExecutionAttemptView:
    return ExecutionAttemptView(
        id=row.id,
        execution_id=row.execution_id,
        attempt_number=row.attempt_number,
        runtime_type=row.runtime_type,
        runtime_profile=row.runtime_profile,
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
        step_count=step_count,
    )


def _operation_view(row: ExecutionOperationORM) -> ExecutionOperationView:
    return ExecutionOperationView(
        id=row.id,
        execution_id=row.execution_id,
        operation_number=row.operation_number,
        schema_version=row.schema_version,
        first_sequence=row.first_sequence,
        last_sequence=row.last_sequence,
        operation_timeout_seconds=row.operation_timeout_seconds,
        metadata=row.operation_metadata,
        status=row.status,
        execution_attempt_id=row.execution_attempt_id,
        error_message=row.error_message,
        created_by_type=row.created_by_type,
        created_by=row.created_by,
        updated_by_type=row.updated_by_type,
        updated_by=row.updated_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        step_count=row.last_sequence - row.first_sequence + 1,
    )


def _step_attempt_view(
    row: ExecutionStepAttemptORM,
) -> ExecutionStepAttemptView:
    return ExecutionStepAttemptView(
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
