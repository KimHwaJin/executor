"""PostgreSQL-backed Executor-wide maintenance control."""

import hashlib
import json

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.application.maintenance import (
    ExecutorMaintenanceView,
    SetExecutorAdmissionCommand,
)
from executor_service.domain.enums import (
    ExecutionStatus,
    ExecutorAdmissionState,
    MaintenanceRunStatus,
    RuntimeSessionCleanupStatus,
)
from executor_service.domain.errors import IdempotencyConflictError
from executor_service.domain.models import utc_now
from executor_service.infrastructure.db.models import (
    CommandReceiptORM,
    ExecutionORM,
    ExecutorMaintenanceORM,
    MaintenanceRunORM,
)

MAINTENANCE_KEY = "executor"
ACTIVE_EXECUTION_STATUSES = (
    ExecutionStatus.DISPATCHED,
    ExecutionStatus.RUNNING,
    ExecutionStatus.WAITING_FOR_OPERATION,
    ExecutionStatus.FINALIZING,
)
UNRESOLVED_CLEANUP_STATUSES = (
    RuntimeSessionCleanupStatus.PENDING,
    RuntimeSessionCleanupStatus.FAILED,
)


class ExecutorMaintenanceService:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        self._session_factory = session_factory

    async def initialize(self) -> None:
        """Support metadata-created development DBs; migrations seed production DBs."""
        async with self._session_factory() as session, session.begin():
            await self._required_state(session)

    async def get(self) -> ExecutorMaintenanceView:
        async with self._session_factory() as session, session.begin():
            state = await self._required_state(session)
            return await self._to_view(session, state)

    async def set_state(
        self, command: SetExecutorAdmissionCommand
    ) -> ExecutorMaintenanceView:
        command_type = (
            f"executor_maintenance.{command.desired_state.value.lower()}"
        )
        fingerprint = _fingerprint(
            {
                "desired_state": command.desired_state.value,
                "actor_type": command.actor_type.value,
                "actor_id": command.actor_id,
            }
        )
        async with self._session_factory() as session, session.begin():
            receipt = await session.scalar(
                select(CommandReceiptORM).where(
                    CommandReceiptORM.idempotency_key
                    == command.idempotency_key
                )
            )
            if receipt is not None:
                if (
                    receipt.command_type != command_type
                    or receipt.request_fingerprint != fingerprint
                ):
                    raise IdempotencyConflictError(
                        "idempotency_key was already used with a different command."
                    )
                state = await self._required_state(session)
                return await self._to_view(session, state)

            state = await self._required_state(session, lock=True)
            if state.admission_state != command.desired_state:
                state.admission_state = command.desired_state
                state.version += 1
            state.updated_by_type = command.actor_type
            state.updated_by = command.actor_id
            state.updated_at = utc_now()
            session.add(
                CommandReceiptORM(
                    idempotency_key=command.idempotency_key,
                    command_type=command_type,
                    request_fingerprint=fingerprint,
                    result={"singleton_key": MAINTENANCE_KEY},
                )
            )
            return await self._to_view(session, state)

    @staticmethod
    async def admission_is_active(
        session: AsyncSession, *, lock: bool = False
    ) -> bool:
        statement = select(ExecutorMaintenanceORM.admission_state).where(
            ExecutorMaintenanceORM.singleton_key == MAINTENANCE_KEY
        )
        if lock:
            statement = statement.with_for_update(read=True)
        state = await session.scalar(statement)
        return state in {None, ExecutorAdmissionState.ACTIVE}

    @staticmethod
    async def _required_state(
        session: AsyncSession, *, lock: bool = False
    ) -> ExecutorMaintenanceORM:
        statement = select(ExecutorMaintenanceORM).where(
            ExecutorMaintenanceORM.singleton_key == MAINTENANCE_KEY
        )
        if lock:
            statement = statement.with_for_update()
        state = await session.scalar(statement)
        if state is None:
            state = ExecutorMaintenanceORM(singleton_key=MAINTENANCE_KEY)
            session.add(state)
            await session.flush()
        return state

    @staticmethod
    async def _to_view(
        session: AsyncSession, state: ExecutorMaintenanceORM
    ) -> ExecutorMaintenanceView:
        queued = await session.scalar(
            select(func.count(ExecutionORM.id)).where(
                ExecutionORM.status == ExecutionStatus.QUEUED
            )
        )
        active = await session.scalar(
            select(func.count(ExecutionORM.id)).where(
                ExecutionORM.status.in_(ACTIVE_EXECUTION_STATUSES)
            )
        )
        cancel_requested = await session.scalar(
            select(func.count(ExecutionORM.id)).where(
                ExecutionORM.status == ExecutionStatus.CANCEL_REQUESTED
            )
        )
        unresolved_cleanup = await session.scalar(
            select(func.count(ExecutionORM.id)).where(
                ExecutionORM.runtime_session_cleanup_status.in_(
                    UNRESOLVED_CLEANUP_STATUSES
                )
            )
        )
        active_runtime_sessions = await session.scalar(
            select(func.count(ExecutionORM.id)).where(
                ExecutionORM.runtime_session_id.is_not(None)
            )
        )
        active_run = await session.scalar(
            select(MaintenanceRunORM)
            .where(
                MaintenanceRunORM.status.in_(
                    (
                        MaintenanceRunStatus.REQUESTED,
                        MaintenanceRunStatus.RUNNING,
                    )
                )
            )
            .order_by(MaintenanceRunORM.created_at.desc())
            .limit(1)
        )
        return ExecutorMaintenanceView(
            admission_state=state.admission_state,
            version=state.version,
            queued_execution_count=queued or 0,
            active_execution_count=active or 0,
            cancel_requested_count=cancel_requested or 0,
            unresolved_cleanup_count=unresolved_cleanup or 0,
            active_runtime_session_count=active_runtime_sessions or 0,
            active_run_id=active_run.id if active_run else None,
            active_run_action=active_run.action if active_run else None,
            active_run_status=active_run.status if active_run else None,
            created_by_type=state.created_by_type,
            created_by=state.created_by,
            updated_by_type=state.updated_by_type,
            updated_by=state.updated_by,
            created_at=state.created_at,
            updated_at=state.updated_at,
        )


def _fingerprint(payload: dict[str, str]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
