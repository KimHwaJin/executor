"""Durable, leased orchestration for Executor Maintenance Runs."""

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.application.commands import CancelExecutionCommand
from executor_service.application.maintenance_runs import (
    CreateMaintenanceRunCommand,
    MaintenanceRunCounts,
    MaintenanceRunTargetView,
    MaintenanceRunView,
)
from executor_service.application.pagination import (
    Page,
    decode_time_cursor,
    encode_time_cursor,
)
from executor_service.application.services import ExecutionService
from executor_service.domain.enums import (
    ActorType,
    ExecutionStatus,
    ExecutorAdmissionState,
    MaintenanceRunStatus,
    MaintenanceRunTargetStatus,
    RuntimeSessionCleanupStatus,
)
from executor_service.domain.errors import (
    ExecutionNotFoundError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    MaintenanceRunConflictError,
    MaintenanceRunNotFoundError,
)
from executor_service.domain.models import utc_now
from executor_service.infrastructure.db.models import (
    ExecutionORM,
    ExecutorMaintenanceORM,
    MaintenanceRunORM,
    MaintenanceRunTargetORM,
)
from executor_service.infrastructure.maintenance import MAINTENANCE_KEY

logger = logging.getLogger(__name__)

RUN_CURSOR_KIND = "maintenance_run_targets"
PROCESS_BATCH_SIZE = 50
RUNNABLE_STATUSES = (
    MaintenanceRunStatus.REQUESTED,
    MaintenanceRunStatus.RUNNING,
)
ACTIVE_EXECUTION_STATUSES = (
    ExecutionStatus.DISPATCHED,
    ExecutionStatus.RUNNING,
    ExecutionStatus.WAITING_FOR_OPERATION,
    ExecutionStatus.FINALIZING,
    ExecutionStatus.CANCEL_REQUESTED,
)
UNRESOLVED_CLEANUP_STATUSES = (
    RuntimeSessionCleanupStatus.PENDING,
    RuntimeSessionCleanupStatus.FAILED,
)


@dataclass(frozen=True, slots=True)
class MaintenanceRunLease:
    run_id: UUID
    owner: str
    fencing_token: int
    actor_type: ActorType | None
    actor_id: str | None


class MaintenanceRunService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        execution_service: ExecutionService,
        *,
        lease_seconds: int,
    ) -> None:
        self._session_factory = session_factory
        self._execution_service = execution_service
        self._lease_seconds = lease_seconds

    async def create(
        self, command: CreateMaintenanceRunCommand
    ) -> MaintenanceRunView:
        fingerprint = _fingerprint(
            {
                "action": command.action.value,
                "actor_type": command.actor_type.value,
                "actor_id": command.actor_id,
            }
        )
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            repeated = await session.scalar(
                select(MaintenanceRunORM).where(
                    MaintenanceRunORM.idempotency_key
                    == command.idempotency_key
                )
            )
            if repeated is not None:
                if repeated.request_fingerprint != fingerprint:
                    raise IdempotencyConflictError(
                        "idempotency_key was already used with a different command."
                    )
                return await self._to_view(session, repeated)

            maintenance = await session.scalar(
                select(ExecutorMaintenanceORM)
                .where(ExecutorMaintenanceORM.singleton_key == MAINTENANCE_KEY)
                .with_for_update()
            )
            if maintenance is None:
                maintenance = ExecutorMaintenanceORM(
                    singleton_key=MAINTENANCE_KEY
                )
                session.add(maintenance)
                await session.flush()

            active_run = await session.scalar(
                select(MaintenanceRunORM.id).where(
                    MaintenanceRunORM.status.in_(RUNNABLE_STATUSES)
                )
            )
            if active_run is not None:
                raise MaintenanceRunConflictError(
                    f"Maintenance Run {active_run} is already active."
                )

            if maintenance.admission_state != ExecutorAdmissionState.DRAINING:
                maintenance.admission_state = ExecutorAdmissionState.DRAINING
                maintenance.version += 1
            maintenance.updated_by_type = command.actor_type
            maintenance.updated_by = command.actor_id
            maintenance.updated_at = now

            selected = list(
                await session.execute(
                    select(ExecutionORM.id, ExecutionORM.status)
                    .where(
                        or_(
                            ExecutionORM.status.in_(ACTIVE_EXECUTION_STATUSES),
                            ExecutionORM.runtime_session_id.is_not(None),
                        )
                    )
                    .order_by(ExecutionORM.created_at, ExecutionORM.id)
                )
            )
            run_id = uuid4()
            status = (
                MaintenanceRunStatus.REQUESTED
                if selected
                else MaintenanceRunStatus.SUCCEEDED
            )
            run = MaintenanceRunORM(
                id=run_id,
                idempotency_key=command.idempotency_key,
                request_fingerprint=fingerprint,
                action=command.action,
                status=status,
                created_by_type=command.actor_type,
                created_by=command.actor_id,
                updated_by_type=command.actor_type,
                updated_by=command.actor_id,
                started_at=now if not selected else None,
                finished_at=now if not selected else None,
            )
            session.add(run)
            session.add_all(
                [
                    MaintenanceRunTargetORM(
                        maintenance_run_id=run_id,
                        execution_id=execution_id,
                        selected_execution_status=execution_status,
                        status=MaintenanceRunTargetStatus.PENDING,
                        created_by_type=command.actor_type,
                        created_by=command.actor_id,
                        updated_by_type=command.actor_type,
                        updated_by=command.actor_id,
                    )
                    for execution_id, execution_status in selected
                ]
            )
            await session.flush()
            return await self._to_view(session, run)

    async def get(self, run_id: UUID) -> MaintenanceRunView:
        async with self._session_factory() as session:
            run = await self._required_run(session, run_id)
            return await self._to_view(session, run)

    async def list_targets(
        self,
        run_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[MaintenanceRunTargetView]:
        async with self._session_factory() as session:
            await self._required_run(session, run_id)
            statement = select(MaintenanceRunTargetORM).where(
                MaintenanceRunTargetORM.maintenance_run_id == run_id
            )
            if cursor is not None:
                created_at, target_id = decode_time_cursor(
                    cursor, RUN_CURSOR_KIND
                )
                statement = statement.where(
                    or_(
                        MaintenanceRunTargetORM.created_at > created_at,
                        and_(
                            MaintenanceRunTargetORM.created_at == created_at,
                            MaintenanceRunTargetORM.id > target_id,
                        ),
                    )
                )
            rows = list(
                await session.scalars(
                    statement.order_by(
                        MaintenanceRunTargetORM.created_at,
                        MaintenanceRunTargetORM.id,
                    ).limit(limit + 1)
                )
            )
            page_rows = rows[:limit]
            next_cursor = None
            if len(rows) > limit and page_rows:
                last = page_rows[-1]
                next_cursor = encode_time_cursor(
                    RUN_CURSOR_KIND, last.created_at, last.id
                )
            return Page(
                items=[self._target_view(row) for row in page_rows],
                next_cursor=next_cursor,
            )

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
                and _as_utc(run.lease_expires_at) > now
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
            counts = await self._counts(session, lease.run_id)
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

    @staticmethod
    async def _required_run(
        session: AsyncSession, run_id: UUID
    ) -> MaintenanceRunORM:
        run = await session.get(MaintenanceRunORM, run_id)
        if run is None:
            raise MaintenanceRunNotFoundError(
                f"Maintenance Run {run_id} was not found."
            )
        return run

    async def _to_view(
        self, session: AsyncSession, run: MaintenanceRunORM
    ) -> MaintenanceRunView:
        return MaintenanceRunView(
            id=run.id,
            action=run.action,
            status=run.status,
            counts=await self._counts(session, run.id),
            error_message=run.error_message,
            created_by_type=run.created_by_type,
            created_by=run.created_by,
            updated_by_type=run.updated_by_type,
            updated_by=run.updated_by,
            created_at=run.created_at,
            updated_at=run.updated_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
        )

    @staticmethod
    async def _counts(
        session: AsyncSession, run_id: UUID
    ) -> MaintenanceRunCounts:
        result = await session.execute(
            select(
                MaintenanceRunTargetORM.status,
                func.count(MaintenanceRunTargetORM.id),
            )
            .where(MaintenanceRunTargetORM.maintenance_run_id == run_id)
            .group_by(MaintenanceRunTargetORM.status)
        )
        grouped = dict(list(result.tuples()))
        return MaintenanceRunCounts(
            total=sum(grouped.values()),
            pending=grouped.get(MaintenanceRunTargetStatus.PENDING, 0),
            stop_requested=grouped.get(
                MaintenanceRunTargetStatus.STOP_REQUESTED, 0
            ),
            stopped=grouped.get(MaintenanceRunTargetStatus.STOPPED, 0),
            failed=grouped.get(MaintenanceRunTargetStatus.FAILED, 0),
        )

    @staticmethod
    def _target_view(
        target: MaintenanceRunTargetORM,
    ) -> MaintenanceRunTargetView:
        return MaintenanceRunTargetView(
            id=target.id,
            maintenance_run_id=target.maintenance_run_id,
            execution_id=target.execution_id,
            selected_execution_status=target.selected_execution_status,
            status=target.status,
            error_message=target.error_message,
            stop_requested_at=target.stop_requested_at,
            completed_at=target.completed_at,
            created_by_type=target.created_by_type,
            created_by=target.created_by,
            updated_by_type=target.updated_by_type,
            updated_by=target.updated_by,
            created_at=target.created_at,
            updated_at=target.updated_at,
        )


def _fingerprint(payload: dict[str, str]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
