"""Cleanup and durable recording for abandoned Runtime sessions."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.domain.diagnostics import DiagnosticCategory
from executor_service.domain.enums import (
    ExecutionStatus,
    RuntimeAbortStatus,
    RuntimeSessionCleanupStatus,
)
from executor_service.domain.models import utc_now
from executor_service.domain.runtime import RuntimeDriver, RuntimeDriverError
from executor_service.infrastructure.background_diagnostics import (
    BackgroundDiagnosticRecorder,
    RuntimeObservation,
)
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.execution_worker.runtime_calls import (
    RuntimeDriverProvider,
)


class RuntimeSessionRecovery:
    """Delete only the captured terminal session and preserve cleanup causes."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        driver_provider: RuntimeDriverProvider,
    ) -> None:
        self._session_factory = session_factory
        self._driver_provider = driver_provider
        self.diagnostics = BackgroundDiagnosticRecorder(session_factory)

    async def cleanup(self, observation: RuntimeObservation) -> None:
        driver: RuntimeDriver | None = None
        phase = "RECOVERY_VALIDATE"
        try:
            async with self._session_factory() as session:
                current = await observation.current(session)
                if (
                    current is None
                    or current.status
                    not in {
                        ExecutionStatus.FAILED,
                        ExecutionStatus.CANCELLED,
                    }
                    or observation.session_id is None
                ):
                    return
                target = (
                    await session.get(RuntimeTargetORM, observation.target_id)
                    if observation.target_id is not None
                    else None
                )
            cleanup_status = RuntimeSessionCleanupStatus.FAILED
            phase = "RECOVERY_TARGET"
            try:
                if target is None:
                    raise RuntimeDriverError(
                        "Cleanup Runtime Target is missing."
                    )
                phase = "RECOVERY_DRIVER_CREATE"
                driver = self._driver_provider.create(target)
                phase = "RECOVERY_SESSION_DELETE"
                await driver.delete_session(observation.session_id)
                cleanup_status = RuntimeSessionCleanupStatus.SUCCEEDED
            except Exception as exc:
                await self.diagnostics.record(
                    observation,
                    exc,
                    phase=phase,
                    category=DiagnosticCategory.CLEANUP,
                )
            finally:
                if driver is not None:
                    try:
                        await driver.close()
                    except Exception as exc:
                        # Client transport close does not undo kernel deletion.
                        await self.diagnostics.record(
                            observation,
                            exc,
                            phase="RECOVERY_DRIVER_CLOSE",
                            category=DiagnosticCategory.CLEANUP,
                        )
            phase = "RECOVERY_RESULT_PERSIST"
            await self.record_result(observation, cleanup_status)
        except Exception as exc:
            # One broken target/DB round-trip must not skip the remaining batch.
            await self.diagnostics.record(
                observation,
                exc,
                phase=phase,
                category=DiagnosticCategory.CLEANUP,
            )

    async def record_result(
        self,
        observation: RuntimeObservation,
        cleanup_status: RuntimeSessionCleanupStatus,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            execution = await observation.current(session, lock=True)
            if execution is None or execution.status not in {
                ExecutionStatus.FAILED,
                ExecutionStatus.CANCELLED,
            }:
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
            attempt = await session.scalar(
                select(ExecutionAttemptORM)
                .where(ExecutionAttemptORM.execution_id == execution.id)
                .order_by(ExecutionAttemptORM.attempt_number.desc())
                .limit(1)
            )
            if attempt is not None:
                attempt.runtime_session_cleanup_status = cleanup_status
                if abort_was_pending:
                    attempt.runtime_abort_status = (
                        execution.runtime_abort_status
                    )
