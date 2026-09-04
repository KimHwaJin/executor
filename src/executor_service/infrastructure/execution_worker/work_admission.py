"""Admission of durable Execution work into the local job dispatcher."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.domain.enums import ExecutionStatus
from executor_service.infrastructure.db.models import ExecutionORM
from executor_service.infrastructure.execution_worker.cancellation import (
    CancellationProcessor,
)
from executor_service.infrastructure.execution_worker.dispatcher import (
    ExecutionJobDispatcher,
)
from executor_service.infrastructure.execution_worker.message_validation import (
    RUN_MESSAGE_TYPES,
)
from executor_service.infrastructure.execution_worker.runner import (
    ExecutionRunner,
)


class WorkAdmissionProcessor:
    """Maps Redis signals and durable DB state to local execution jobs."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        dispatcher: ExecutionJobDispatcher,
        runner: ExecutionRunner,
        cancellation: CancellationProcessor,
    ) -> None:
        self._session_factory = session_factory
        self._dispatcher = dispatcher
        self._runner = runner
        self._cancellation = cancellation

    async def handle_message(self, fields: dict[str, str]) -> bool:
        """Dispatch one validated Redis work message."""
        message_type = fields.get("message_type")
        execution_id = UUID(fields["aggregate_id"])
        if message_type in RUN_MESSAGE_TYPES:
            self._dispatcher.dispatch(
                execution_id,
                self._runner.run(execution_id),
            )
        elif message_type == "execution.cancellation_ready":
            self._dispatcher.dispatch(
                execution_id,
                self._cancellation.cancel(execution_id),
                replace=True,
            )
        else:
            return False
        return True

    async def reconcile(self) -> int:
        """Redis-independent admission from PostgreSQL source-of-truth state."""
        async with self._session_factory() as session:
            rows = list(
                await session.execute(
                    select(
                        ExecutionORM.id,
                        ExecutionORM.status,
                    )
                    .where(
                        ExecutionORM.status.in_(
                            [
                                ExecutionStatus.QUEUED,
                                ExecutionStatus.FINALIZING,
                                ExecutionStatus.CANCEL_REQUESTED,
                            ]
                        )
                    )
                    .order_by(ExecutionORM.created_at)
                    .limit(100)
                )
            )
        for execution_id, status in rows:
            self._dispatch_durable_state(execution_id, status)
        return len(rows)

    def _dispatch_durable_state(
        self,
        execution_id: UUID,
        status: ExecutionStatus,
    ) -> None:
        if status == ExecutionStatus.CANCEL_REQUESTED:
            self._dispatcher.dispatch(
                execution_id,
                self._cancellation.cancel(execution_id),
                replace=True,
            )
            return
        self._dispatcher.dispatch(
            execution_id,
            self._runner.run(execution_id),
        )
