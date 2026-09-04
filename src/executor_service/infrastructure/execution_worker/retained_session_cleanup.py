"""Retention-window cleanup for failed Runtime sessions."""

from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.domain.enums import (
    ExecutionStatus,
    RetryStrategy,
    RuntimeSessionCleanupStatus,
    StepStatus,
)
from executor_service.domain.models import utc_now
from executor_service.infrastructure.background_diagnostics import (
    RuntimeObservation,
)
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionORM,
    ExecutionStepORM,
)
from executor_service.infrastructure.execution_worker.event_writer import (
    add_execution_completed_event,
)
from executor_service.infrastructure.execution_worker.execution_state import (
    fail_active_operation_without_attempt,
)
from executor_service.infrastructure.execution_worker.session_recovery import (
    RuntimeSessionRecovery,
)
from executor_service.settings import Settings


class RetainedSessionCleaner:
    """Reserve expired sessions before deletion; retry unresolved cleanup."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        session_recovery: RuntimeSessionRecovery,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._session_recovery = session_recovery

    async def reconcile(self) -> None:
        await self.cleanup_expired()
        await self.retry_unresolved()

    async def retry_unresolved(self) -> None:
        now = utc_now()
        retry_before = now - timedelta(
            seconds=self._settings.runtime_cleanup_retry_interval_seconds
        )
        cleanup_targets: list[RuntimeObservation] = []
        async with self._session_factory() as session, session.begin():
            executions = list(
                await session.scalars(
                    select(ExecutionORM)
                    .where(
                        ExecutionORM.status.in_(
                            [ExecutionStatus.FAILED, ExecutionStatus.CANCELLED]
                        ),
                        ExecutionORM.runtime_session_id.is_not(None),
                        ExecutionORM.runtime_session_cleanup_status.in_(
                            [
                                RuntimeSessionCleanupStatus.PENDING,
                                RuntimeSessionCleanupStatus.FAILED,
                            ]
                        ),
                        ExecutionORM.updated_at <= retry_before,
                    )
                    .order_by(ExecutionORM.updated_at)
                    .limit(20)
                    .with_for_update(skip_locked=True)
                )
            )
            for execution in executions:
                execution.runtime_session_cleanup_status = (
                    RuntimeSessionCleanupStatus.PENDING
                )
                execution.updated_at = now
                execution.version += 1
                await self._mark_attempt_pending(session, execution)
                cleanup_targets.append(RuntimeObservation.capture(execution))
        for observation in cleanup_targets:
            await self._session_recovery.cleanup(observation)

    async def cleanup_expired(self) -> None:
        now = utc_now()
        cleanup_targets: list[RuntimeObservation] = []
        async with self._session_factory() as session, session.begin():
            executions = list(
                await session.scalars(
                    select(ExecutionORM)
                    .where(
                        ExecutionORM.status.in_(
                            [ExecutionStatus.FAILED, ExecutionStatus.QUEUED]
                        ),
                        ExecutionORM.retry_strategy
                        == RetryStrategy.FROM_FAILED_STEP,
                        ExecutionORM.retained_runtime_session_until <= now,
                        ExecutionORM.runtime_session_id.is_not(None),
                    )
                    .order_by(ExecutionORM.retained_runtime_session_until)
                    .limit(20)
                    .with_for_update(skip_locked=True)
                )
            )
            for execution in executions:
                retry_was_queued = execution.status == ExecutionStatus.QUEUED
                if retry_was_queued:
                    execution.status = ExecutionStatus.FAILED
                    execution.error_message = (
                        "The retained runtime session retry window expired before "
                        "execution resumed."
                    )
                    execution.finished_at = now
                    await session.execute(
                        update(ExecutionStepORM)
                        .where(
                            ExecutionStepORM.execution_id == execution.id,
                            ExecutionStepORM.status == StepStatus.PENDING,
                        )
                        .values(
                            status=StepStatus.SKIPPED,
                            finished_at=now,
                            updated_at=now,
                        )
                    )
                # Commit the reservation before any remote delete. A concurrent
                # retry/claim can no longer resume this expiring kernel.
                execution.retry_strategy = RetryStrategy.NOT_RETRYABLE
                execution.retry_from_sequence = None
                execution.retained_runtime_session_until = None
                execution.runtime_session_cleanup_status = (
                    RuntimeSessionCleanupStatus.PENDING
                )
                execution.updated_at = now
                execution.version += 1
                if retry_was_queued:
                    await fail_active_operation_without_attempt(
                        session,
                        execution,
                        now,
                        execution.error_message
                        or "Retained Runtime retry window expired.",
                    )
                await self._mark_attempt_pending(session, execution)
                cleanup_targets.append(RuntimeObservation.capture(execution))
                if retry_was_queued:
                    await add_execution_completed_event(session, execution.id)
        for observation in cleanup_targets:
            await self._session_recovery.cleanup(observation)

    @staticmethod
    async def _mark_attempt_pending(
        session: AsyncSession, execution: ExecutionORM
    ) -> None:
        attempt_id = await session.scalar(
            select(ExecutionAttemptORM.id)
            .where(ExecutionAttemptORM.execution_id == execution.id)
            .order_by(ExecutionAttemptORM.attempt_number.desc())
            .limit(1)
        )
        if attempt_id is not None:
            await session.execute(
                update(ExecutionAttemptORM)
                .where(ExecutionAttemptORM.id == attempt_id)
                .values(
                    runtime_session_cleanup_status=RuntimeSessionCleanupStatus.PENDING
                )
            )
