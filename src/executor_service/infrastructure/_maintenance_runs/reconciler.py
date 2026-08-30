"""Leased reconciliation of Executor Maintenance Run targets."""

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.application.commands import CancelExecutionCommand
from executor_service.application.services import ExecutionService
from executor_service.domain.enums import (
    ExecutionStatus,
    MaintenanceRunStatus,
    MaintenanceRunTargetStatus,
)
from executor_service.domain.errors import (
    ExecutionNotFoundError,
    InvalidStateTransitionError,
)
from executor_service.domain.models import utc_now
from executor_service.infrastructure._maintenance_runs.constants import (
    PROCESS_BATCH_SIZE,
    RUNNABLE_STATUSES,
    UNRESOLVED_CLEANUP_STATUSES,
)
from executor_service.infrastructure._maintenance_runs.queries import (
    MaintenanceRunQueries,
)
from executor_service.infrastructure._maintenance_runs.types import (
    MaintenanceRunLease,
)
from executor_service.infrastructure.db.models import (
    ExecutionORM,
    MaintenanceRunORM,
    MaintenanceRunTargetORM,
)

logger = logging.getLogger(__name__)


class MaintenanceRunReconciler:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        execution_service: ExecutionService,
        queries: MaintenanceRunQueries,
        *,
        lease_seconds: int,
    ) -> None:
        self._session_factory = session_factory
        self._execution_service = execution_service
        self._queries = queries
        self._lease_seconds = lease_seconds

    async def reconcile_once(self, owner: str) -> bool:
        lease = await self._claim(owner)
        if lease is None:
            return False
        target_ids = await self._pending_target_ids(lease.run_id)
        for target_id, execution_id in target_ids:
            if not await self._renew(lease):
                return True
            try:
                await self._execution_service.cancel(
                    CancelExecutionCommand(
                        execution_id=execution_id,
                        idempotency_key=(
                            f"maintenance-run:{lease.run_id}:{execution_id}"
                        ),
                        reason=(
                            "Stopped by Executor Maintenance Run "
                            f"{lease.run_id}."
                        ),
                        actor_type=lease.actor_type,
                        actor_id=lease.actor_id,
                    )
                )
            except (ExecutionNotFoundError, InvalidStateTransitionError):
                pass
            except Exception as exc:
                logger.warning(
                    "Maintenance Run cancellation request failed",
                    extra={
                        "maintenance_run_id": str(lease.run_id),
                        "execution_id": str(execution_id),
                    },
                    exc_info=exc,
                )
                await self._record_target_error(lease, target_id, str(exc))
                continue
            await self._mark_stop_requested(lease, target_id)
        await self._refresh_and_finalize(lease)
        return True

    async def _claim(self, owner: str) -> MaintenanceRunLease | None:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            run = await session.scalar(
                select(MaintenanceRunORM)
                .where(
                    MaintenanceRunORM.status.in_(RUNNABLE_STATUSES),
                    or_(
                        MaintenanceRunORM.lease_owner.is_(None),
                        MaintenanceRunORM.lease_owner == owner,
                        MaintenanceRunORM.lease_expires_at.is_(None),
                        MaintenanceRunORM.lease_expires_at <= now,
                    ),
                )
                .order_by(MaintenanceRunORM.created_at)
                .with_for_update(skip_locked=True)
            )
            if run is None:
                return None
            active_owner = (
                run.lease_owner is not None
                and run.lease_expires_at is not None
                and as_utc(run.lease_expires_at) > now
            )
            if not active_owner:
                run.fencing_token += 1
            run.lease_owner = owner
            run.lease_expires_at = now + timedelta(seconds=self._lease_seconds)
            run.heartbeat_at = now
            run.status = MaintenanceRunStatus.RUNNING
            run.started_at = run.started_at or now
            run.updated_at = now
            return MaintenanceRunLease(
                run_id=run.id,
                owner=owner,
                fencing_token=run.fencing_token,
                actor_type=run.created_by_type,
                actor_id=run.created_by,
            )

    async def _pending_target_ids(
        self, run_id: UUID
    ) -> list[tuple[UUID, UUID]]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(
                    MaintenanceRunTargetORM.id,
                    MaintenanceRunTargetORM.execution_id,
                )
                .where(
                    MaintenanceRunTargetORM.maintenance_run_id == run_id,
                    MaintenanceRunTargetORM.status
                    == MaintenanceRunTargetStatus.PENDING,
                )
                .order_by(MaintenanceRunTargetORM.created_at)
                .limit(PROCESS_BATCH_SIZE)
            )
            return list(result.tuples())

    async def _renew(self, lease: MaintenanceRunLease) -> bool:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            run = await self._owned_run(session, lease)
            if run is None:
                return False
            run.lease_expires_at = now + timedelta(seconds=self._lease_seconds)
            run.heartbeat_at = now
            run.updated_at = now
            return True

    async def _mark_stop_requested(
        self, lease: MaintenanceRunLease, target_id: UUID
    ) -> None:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            if await self._owned_run(session, lease) is None:
                return
            target = await session.get(MaintenanceRunTargetORM, target_id)
            if (
                target is None
                or target.status != MaintenanceRunTargetStatus.PENDING
            ):
                return
            target.status = MaintenanceRunTargetStatus.STOP_REQUESTED
            target.stop_requested_at = now
            target.error_message = None
            target.updated_at = now

    async def _record_target_error(
        self, lease: MaintenanceRunLease, target_id: UUID, error: str
    ) -> None:
        async with self._session_factory() as session, session.begin():
            if await self._owned_run(session, lease) is None:
                return
            target = await session.get(MaintenanceRunTargetORM, target_id)
            if target is not None:
                target.error_message = error[:2000]
                target.updated_at = utc_now()

    async def _refresh_and_finalize(self, lease: MaintenanceRunLease) -> None:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            run = await self._owned_run(session, lease)
            if run is None:
                return
            rows = list(
                await session.execute(
                    select(MaintenanceRunTargetORM, ExecutionORM)
                    .join(
                        ExecutionORM,
                        ExecutionORM.id
                        == MaintenanceRunTargetORM.execution_id,
                    )
                    .where(
                        MaintenanceRunTargetORM.maintenance_run_id
                        == lease.run_id,
                        MaintenanceRunTargetORM.status.in_(
                            (
                                MaintenanceRunTargetStatus.PENDING,
                                MaintenanceRunTargetStatus.STOP_REQUESTED,
                            )
                        ),
                    )
                    .with_for_update()
                )
            )
            for target, execution in rows:
                if (
                    execution.status.is_terminal
                    and execution.runtime_session_id is None
                    and execution.runtime_session_cleanup_status
                    not in UNRESOLVED_CLEANUP_STATUSES
                ):
                    target.status = MaintenanceRunTargetStatus.STOPPED
                    target.completed_at = now
                    target.error_message = None
                    target.updated_at = now
                elif execution.status == ExecutionStatus.CANCEL_REQUESTED:
                    target.status = MaintenanceRunTargetStatus.STOP_REQUESTED
                    target.stop_requested_at = target.stop_requested_at or now
                    target.updated_at = now

            await session.flush()
            counts = await self._queries.counts(session, lease.run_id)
            if counts.remaining == 0:
                run.status = (
                    MaintenanceRunStatus.FAILED
                    if counts.failed
                    else MaintenanceRunStatus.SUCCEEDED
                )
                run.finished_at = now
                run.lease_owner = None
                run.lease_expires_at = None
                run.heartbeat_at = None
            else:
                run.lease_expires_at = now + timedelta(
                    seconds=self._lease_seconds
                )
                run.heartbeat_at = now
            run.updated_at = now

    @staticmethod
    async def _owned_run(
        session: AsyncSession, lease: MaintenanceRunLease
    ) -> MaintenanceRunORM | None:
        now = utc_now()
        return await session.scalar(
            select(MaintenanceRunORM)
            .where(
                MaintenanceRunORM.id == lease.run_id,
                MaintenanceRunORM.status == MaintenanceRunStatus.RUNNING,
                MaintenanceRunORM.lease_owner == lease.owner,
                MaintenanceRunORM.fencing_token == lease.fencing_token,
                MaintenanceRunORM.lease_expires_at > now,
            )
            .with_for_update()
        )


def as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
