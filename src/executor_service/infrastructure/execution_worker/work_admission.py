"""Admission of durable Execution work into the local job dispatcher."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select, tuple_
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
        self._reconcile_cursor: tuple[datetime, UUID] | None = None
        self._reconcile_upper_bound: tuple[datetime, UUID] | None = None

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
        query = select(
            ExecutionORM.id, ExecutionORM.status, ExecutionORM.created_at
        ).where(
            ExecutionORM.status.in_(
                [
                    ExecutionStatus.QUEUED,
                    ExecutionStatus.FINALIZING,
                    ExecutionStatus.CANCEL_REQUESTED,
                ]
            )
        )
        if self._reconcile_cursor is not None:
            query = query.where(
                tuple_(ExecutionORM.created_at, ExecutionORM.id)
                > self._reconcile_cursor
            )
        async with self._session_factory() as session:
            if self._reconcile_upper_bound is None:
                newest = (
                    await session.execute(
                        query.with_only_columns(
                            ExecutionORM.created_at, ExecutionORM.id
                        )
                        .order_by(
                            ExecutionORM.created_at.desc(),
                            ExecutionORM.id.desc(),
                        )
                        .limit(1)
                    )
                ).first()
                if newest is None:
                    self._reconcile_cursor = None
                    return 0
                self._reconcile_upper_bound = (newest[0], newest[1])
            # Bound the pass to the original tail. Continuous new submissions
            # must not prevent us from returning to older waiting work.
            query = query.where(
                tuple_(ExecutionORM.created_at, ExecutionORM.id)
                <= self._reconcile_upper_bound
            )
            rows = list(
                await session.execute(
                    query.order_by(
                        ExecutionORM.created_at, ExecutionORM.id
                    ).limit(100)
                )
            )
        self._reconcile_cursor = (
            (rows[-1][2], rows[-1][0]) if len(rows) == 100 else None
        )
        if self._reconcile_cursor is None:
            self._reconcile_upper_bound = None
        for execution_id, status, _created_at in rows:
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
