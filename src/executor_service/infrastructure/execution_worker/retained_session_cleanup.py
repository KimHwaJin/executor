"""Retention-window cleanup for failed Runtime sessions."""

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.config import Settings
from executor_service.domain.enums import (
    ExecutionStatus,
    RetryStrategy,
    RuntimeSessionCleanupStatus,
    StepStatus,
)
from executor_service.domain.models import utc_now
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionORM,
    ExecutionStepORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.execution_worker.event_writer import (
    add_execution_completed_event,
)
from executor_service.infrastructure.execution_worker.execution_state import (
    fail_active_operation_without_attempt,
)
from executor_service.infrastructure.execution_worker.runtime_calls import (
    RuntimeDriverProvider,
)
from executor_service.infrastructure.execution_worker.session_recovery import (
    RuntimeSessionRecovery,
)

logger = logging.getLogger(__name__)


class RetainedSessionCleaner:
    """Cleans failed retained sessions and retries unresolved cleanup."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        driver_provider: RuntimeDriverProvider,
        session_recovery: RuntimeSessionRecovery,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._driver_provider = driver_provider
        self._session_recovery = session_recovery

    async def reconcile(self) -> None:
        await self.cleanup_expired()
        await self.retry_unresolved()

    async def retry_unresolved(self) -> None:
        now = utc_now()
        retry_before = now - timedelta(
            seconds=self._settings.runtime_cleanup_retry_interval_seconds
        )
        cleanup_targets: list[tuple[UUID, UUID | None, UUID | None, str]] = []
        async with self._session_factory() as session, session.begin():
            executions = list(
                await session.scalars(
                    select(ExecutionORM)
                    .where(
                        ExecutionORM.status.in_(
                            [
                                ExecutionStatus.FAILED,
                                ExecutionStatus.CANCELLED,
                            ]
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
                runtime_session_id = execution.runtime_session_id
                if runtime_session_id is None:
                    continue
                attempt_id = await session.scalar(
                    select(ExecutionAttemptORM.id)
                    .where(ExecutionAttemptORM.execution_id == execution.id)
                    .order_by(ExecutionAttemptORM.attempt_number.desc())
                    .limit(1)
                )
                execution.runtime_session_cleanup_status = (
                    RuntimeSessionCleanupStatus.PENDING
                )
                execution.updated_at = now
                execution.version += 1
                if attempt_id is not None:
                    await session.execute(
                        update(ExecutionAttemptORM)
                        .where(ExecutionAttemptORM.id == attempt_id)
                        .values(
                            runtime_session_cleanup_status=(
                                RuntimeSessionCleanupStatus.PENDING
                            )
                        )
                    )
                cleanup_targets.append(
                    (
                        execution.id,
                        attempt_id,
                        execution.runtime_target_id,
                        runtime_session_id,
                    )
                )
        for execution_id, attempt_id, target_id, session_id in cleanup_targets:
            if target_id is None:
                await self._session_recovery.record_result(
                    execution_id,
                    attempt_id,
                    session_id,
                    RuntimeSessionCleanupStatus.FAILED,
                )
                continue
            await self._session_recovery.cleanup(
                execution_id,
                attempt_id,
                target_id,
                session_id,
            )

    async def cleanup_expired(self) -> None:
        now = utc_now()
        async with self._session_factory() as session:
            rows = list(
                await session.execute(
                    select(ExecutionORM, RuntimeTargetORM)
                    .join(
                        RuntimeTargetORM,
                        RuntimeTargetORM.id == ExecutionORM.runtime_target_id,
                    )
                    .where(
                        ExecutionORM.status.in_(
                            [ExecutionStatus.FAILED, ExecutionStatus.QUEUED]
                        ),
                        ExecutionORM.retry_strategy
                        == RetryStrategy.FROM_FAILED_STEP,
                        ExecutionORM.retained_runtime_session_until <= now,
                        ExecutionORM.runtime_session_id.is_not(None),
                    )
                )
            )
        for execution, target in rows:
            driver = self._driver_provider.create(target)
            cleanup_status = RuntimeSessionCleanupStatus.SUCCEEDED
            try:
                if execution.runtime_session_id is not None:
                    await driver.delete_session(execution.runtime_session_id)
            except Exception:
                cleanup_status = RuntimeSessionCleanupStatus.FAILED
                logger.warning(
                    "Expired retained runtime session cleanup failed",
                    extra={"execution_id": str(execution.id)},
                )
            finally:
                await driver.close()
            async with (
                self._session_factory() as update_session,
                update_session.begin(),
            ):
                current = await update_session.scalar(
                    select(ExecutionORM)
                    .where(ExecutionORM.id == execution.id)
                    .with_for_update()
                )
                if (
                    current is None
                    or current.status
                    not in {ExecutionStatus.FAILED, ExecutionStatus.QUEUED}
                    or current.retry_strategy != RetryStrategy.FROM_FAILED_STEP
                    or current.retained_runtime_session_until is None
                    or _as_utc(current.retained_runtime_session_until) > now
                ):
                    continue
                retry_was_queued = current.status == ExecutionStatus.QUEUED
                if retry_was_queued:
                    current.status = ExecutionStatus.FAILED
                    current.error_message = (
                        "The retained runtime session retry window expired before "
                        "execution resumed."
                    )
                    current.finished_at = now
                    await update_session.execute(
                        update(ExecutionStepORM)
                        .where(
                            ExecutionStepORM.execution_id == current.id,
                            ExecutionStepORM.status == StepStatus.PENDING,
                        )
                        .values(
                            status=StepStatus.SKIPPED,
                            finished_at=now,
                            updated_at=now,
                        )
                    )
                current.retry_strategy = RetryStrategy.NOT_RETRYABLE
                current.retry_from_sequence = None
                current.retained_runtime_session_until = None
                if cleanup_status == RuntimeSessionCleanupStatus.SUCCEEDED:
                    current.runtime_session_id = None
                current.runtime_session_cleanup_status = cleanup_status
                current.updated_at = now
                current.version += 1
                if retry_was_queued:
                    await fail_active_operation_without_attempt(
                        update_session,
                        current,
                        now,
                        current.error_message
                        or "The retained Runtime session retry window expired.",
                    )
                latest_attempt_id = await update_session.scalar(
                    select(ExecutionAttemptORM.id)
                    .where(ExecutionAttemptORM.execution_id == current.id)
                    .order_by(ExecutionAttemptORM.attempt_number.desc())
                    .limit(1)
                )
                if latest_attempt_id is not None:
                    await update_session.execute(
                        update(ExecutionAttemptORM)
                        .where(ExecutionAttemptORM.id == latest_attempt_id)
                        .values(runtime_session_cleanup_status=cleanup_status)
                    )
                if retry_was_queued:
                    await add_execution_completed_event(
                        update_session, current.id
                    )


def _as_utc(value: datetime) -> datetime:
    """SQLite tests may return timezone-naive values for aware columns."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
