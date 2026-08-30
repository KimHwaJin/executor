"""Cleanup and durable recording for abandoned Runtime sessions."""

import logging
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.domain.enums import (
    ExecutionStatus,
    RuntimeAbortStatus,
    RuntimeSessionCleanupStatus,
)
from executor_service.domain.models import utc_now
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.execution_worker.runtime_calls import (
    RuntimeDriverProvider,
)

logger = logging.getLogger(__name__)


class RuntimeSessionRecovery:
    """Deletes abandoned sessions and records the fenced cleanup result."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        driver_provider: RuntimeDriverProvider,
    ) -> None:
        self._session_factory = session_factory
        self._driver_provider = driver_provider

    async def cleanup(
        self,
        execution_id: UUID,
        attempt_id: UUID | None,
        target_id: UUID,
        runtime_session_id: str,
    ) -> None:
        async with self._session_factory() as session:
            target = await session.get(RuntimeTargetORM, target_id)
        if target is None:
            await self.record_result(
                execution_id,
                attempt_id,
                runtime_session_id,
                RuntimeSessionCleanupStatus.FAILED,
            )
            return
        try:
            driver = self._driver_provider.create(target)
        except Exception:
            logger.warning(
                "Abandoned runtime session cleanup could not create a driver",
                extra={"execution_id": str(execution_id)},
                exc_info=True,
            )
            await self.record_result(
                execution_id,
                attempt_id,
                runtime_session_id,
                RuntimeSessionCleanupStatus.FAILED,
            )
            return
        try:
            await driver.delete_session(runtime_session_id)
        except Exception:
            logger.warning(
                "Abandoned runtime session cleanup failed",
                extra={"execution_id": str(execution_id)},
            )
            cleanup_status = RuntimeSessionCleanupStatus.FAILED
        else:
            cleanup_status = RuntimeSessionCleanupStatus.SUCCEEDED
        finally:
            await driver.close()
        await self.record_result(
            execution_id,
            attempt_id,
            runtime_session_id,
            cleanup_status,
        )

    async def record_result(
        self,
        execution_id: UUID,
        attempt_id: UUID | None,
        runtime_session_id: str,
        cleanup_status: RuntimeSessionCleanupStatus,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            execution = await session.scalar(
                select(ExecutionORM)
                .where(
                    ExecutionORM.id == execution_id,
                    ExecutionORM.status.in_(
                        [
                            ExecutionStatus.FAILED,
                            ExecutionStatus.CANCELLED,
                        ]
                    ),
                    ExecutionORM.runtime_session_id == runtime_session_id,
                )
                .with_for_update()
            )
            if execution is None:
                return
            abort_was_pending = (
                execution.runtime_abort_status == RuntimeAbortStatus.PENDING
            )
            if cleanup_status == RuntimeSessionCleanupStatus.SUCCEEDED:
                execution.runtime_session_id = None
            execution.runtime_session_cleanup_status = cleanup_status
            if abort_was_pending:
                execution.runtime_abort_status = (
                    RuntimeAbortStatus.SESSION_DELETED
                    if cleanup_status == RuntimeSessionCleanupStatus.SUCCEEDED
                    else RuntimeAbortStatus.FAILED
                )
            execution.updated_at = utc_now()
            execution.version += 1
            if attempt_id is not None:
                await session.execute(
                    update(ExecutionAttemptORM)
                    .where(ExecutionAttemptORM.id == attempt_id)
                    .values(
                        runtime_session_cleanup_status=cleanup_status,
                        **(
                            {
                                "runtime_abort_status": (
                                    RuntimeAbortStatus.SESSION_DELETED
                                    if cleanup_status
                                    == RuntimeSessionCleanupStatus.SUCCEEDED
                                    else RuntimeAbortStatus.FAILED
                                )
                            }
                            if abort_was_pending
                            else {}
                        ),
                    )
                )
