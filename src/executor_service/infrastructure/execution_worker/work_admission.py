"""Admission of durable Execution work into the local job dispatcher."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.domain.enums import ExecutionStatus
from executor_service.infrastructure.db.models import (
    ExecutionHandoverORM,
    ExecutionORM,
)
from executor_service.infrastructure.execution_worker.cancellation import (
    CancellationProcessor,
)
from executor_service.infrastructure.execution_worker.dispatcher import (
    ExecutionJobDispatcher,
)
from executor_service.infrastructure.execution_worker.runner import (
    ExecutionRunner,
)


class WorkAdmissionProcessor:
    """Polls PostgreSQL, the source of truth, for work this Worker can take.

    Cancellations and released Steps are always dispatched: they need no new
    Runtime capacity. New runs are limited to the Worker's free slots so a
    backlog costs each Pod a bounded number of claims per pass, and work
    spreads across replicas.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        dispatcher: ExecutionJobDispatcher,
        runner: ExecutionRunner,
        cancellation: CancellationProcessor,
        *,
        max_active_jobs: int,
    ) -> None:
        self._session_factory = session_factory
        self._dispatcher = dispatcher
        self._runner = runner
        self._cancellation = cancellation
        self._max_active_jobs = max_active_jobs
        self._run_cursor: tuple[datetime, UUID] | None = None

    async def reconcile(self) -> int:
        async with self._session_factory() as session:
            urgent = list(
                await session.execute(
                    select(ExecutionORM.id, ExecutionORM.status).where(
                        or_(
                            ExecutionORM.status
                            == ExecutionStatus.CANCEL_REQUESTED,
                            # A running Step released for another Worker.
                            and_(
                                ExecutionORM.status == ExecutionStatus.RUNNING,
                                ExecutionORM.id.in_(
                                    select(ExecutionHandoverORM.execution_id)
                                ),
                            ),
                        )
                    )
                )
            )
            for execution_id, status in urgent:
                if status == ExecutionStatus.CANCEL_REQUESTED:
                    self._dispatcher.dispatch(
                        execution_id,
                        self._cancellation.cancel(execution_id),
                        replace=True,
                    )
                else:
                    self._dispatcher.dispatch(
                        execution_id, self._runner.run(execution_id)
                    )
            free_slots = (
                self._max_active_jobs - self._dispatcher.active_job_count
            )
            if free_slots <= 0:
                return len(urgent)
            runs = await self._next_runs(session, free_slots)
        for execution_id in runs:
            self._dispatcher.dispatch(
                execution_id, self._runner.run(execution_id)
            )
        return len(urgent) + len(runs)

    async def _next_runs(
        self, session: AsyncSession, limit: int
    ) -> list[UUID]:
        """Oldest waiting runs, rotating so blocked ones cannot starve others."""

        query = (
            select(ExecutionORM.created_at, ExecutionORM.id)
            .where(
                ExecutionORM.status.in_(
                    [ExecutionStatus.QUEUED, ExecutionStatus.FINALIZING]
                )
            )
            .order_by(ExecutionORM.created_at, ExecutionORM.id)
            .limit(limit)
        )
        if self._run_cursor is not None:
            query = query.where(
                tuple_(ExecutionORM.created_at, ExecutionORM.id)
                > self._run_cursor
            )
        rows = list(await session.execute(query))
        self._run_cursor = (
            (rows[-1][0], rows[-1][1]) if len(rows) == limit else None
        )
        return [execution_id for _created_at, execution_id in rows]
