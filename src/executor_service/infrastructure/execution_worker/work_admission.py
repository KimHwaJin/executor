"""Admission of durable Execution work into the local job dispatcher."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import ColumnElement, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import InstrumentedAttribute

from executor_service.domain.enums import ExecutionStatus
from executor_service.domain.models import utc_now
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

    Cancellations need no Runtime capacity, so every one this Worker may
    claim is dispatched; one still running under another Worker's lease is
    left to that Worker. Released Steps and then new runs share the
    Worker's free slots, so a backlog or a scale-in costs each Pod a
    bounded number of claims per pass, and work spreads across replicas.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        dispatcher: ExecutionJobDispatcher,
        runner: ExecutionRunner,
        cancellation: CancellationProcessor,
        *,
        worker_id: str,
        max_active_jobs: int,
    ) -> None:
        self._session_factory = session_factory
        self._dispatcher = dispatcher
        self._runner = runner
        self._cancellation = cancellation
        self._worker_id = worker_id
        self._max_active_jobs = max_active_jobs
        self._run_cursor: tuple[datetime, UUID] | None = None

    async def reconcile(self) -> int:
        async with self._session_factory() as session:
            cancellations = await self._claimable_cancellations(session)
            for execution_id in cancellations:
                self._dispatcher.dispatch(
                    execution_id,
                    self._cancellation.cancel(execution_id),
                    replace=True,
                )
            free_slots = (
                self._max_active_jobs - self._dispatcher.active_job_count
            )
            if free_slots <= 0:
                return len(cancellations)
            # A released Step keeps running on its kernel and its journal
            # keeps the output, so waiting for a slot loses nothing.
            handovers = await self._oldest_handovers(session, free_slots)
            runs = await self._next_runs(session, free_slots - len(handovers))
        for execution_id in (*handovers, *runs):
            self._dispatcher.dispatch(
                execution_id, self._runner.run(execution_id)
            )
        return len(cancellations) + len(handovers) + len(runs)

    async def _claimable_cancellations(
        self, session: AsyncSession
    ) -> list[UUID]:
        now = utc_now()
        return list(
            await session.scalars(
                select(ExecutionORM.id).where(
                    ExecutionORM.status == ExecutionStatus.CANCEL_REQUESTED,
                    _free_or_mine(
                        ExecutionORM.lease_owner,
                        ExecutionORM.lease_expires_at,
                        self._worker_id,
                        now,
                    ),
                    _free_or_mine(
                        ExecutionORM.cancellation_lease_owner,
                        ExecutionORM.cancellation_lease_expires_at,
                        self._worker_id,
                        now,
                    ),
                )
            )
        )

    async def _oldest_handovers(
        self, session: AsyncSession, limit: int
    ) -> list[UUID]:
        return list(
            await session.scalars(
                select(ExecutionORM.id)
                .join(
                    ExecutionHandoverORM,
                    ExecutionHandoverORM.execution_id == ExecutionORM.id,
                )
                .where(ExecutionORM.status == ExecutionStatus.RUNNING)
                .order_by(
                    ExecutionHandoverORM.paused_since,
                    ExecutionHandoverORM.execution_id,
                )
                .limit(limit)
            )
        )

    async def _next_runs(
        self, session: AsyncSession, limit: int
    ) -> list[UUID]:
        """Oldest waiting runs, rotating so blocked ones cannot starve others."""

        if limit <= 0:
            return []

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


def _free_or_mine(
    owner: InstrumentedAttribute[str | None],
    expires_at: InstrumentedAttribute[datetime | None],
    worker_id: str,
    now: datetime,
) -> ColumnElement[bool]:
    return or_(
        owner.is_(None),
        owner == worker_id,
        expires_at.is_(None),
        expires_at <= now,
    )
