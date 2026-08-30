"""Transactional creation of Executor Maintenance Runs."""

import hashlib
import json
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.application.maintenance_runs import (
    CreateMaintenanceRunCommand,
    MaintenanceRunView,
)
from executor_service.domain.enums import (
    ExecutorAdmissionState,
    MaintenanceRunStatus,
    MaintenanceRunTargetStatus,
)
from executor_service.domain.errors import (
    IdempotencyConflictError,
    MaintenanceRunConflictError,
)
from executor_service.domain.models import utc_now
from executor_service.infrastructure._maintenance_runs.constants import (
    ACTIVE_EXECUTION_STATUSES,
    RUNNABLE_STATUSES,
)
from executor_service.infrastructure._maintenance_runs.queries import (
    MaintenanceRunQueries,
)
from executor_service.infrastructure.db.models import (
    ExecutionORM,
    ExecutorMaintenanceORM,
    MaintenanceRunORM,
    MaintenanceRunTargetORM,
)
from executor_service.infrastructure.maintenance import MAINTENANCE_KEY


class MaintenanceRunCommands:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        queries: MaintenanceRunQueries,
    ) -> None:
        self._session_factory = session_factory
        self._queries = queries

    async def create(
        self, command: CreateMaintenanceRunCommand
    ) -> MaintenanceRunView:
        request_fingerprint = fingerprint(
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
                if repeated.request_fingerprint != request_fingerprint:
                    raise IdempotencyConflictError(
                        "idempotency_key was already used with a different "
                        "command."
                    )
                return await self._queries.view(session, repeated)

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
                request_fingerprint=request_fingerprint,
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
            return await self._queries.view(session, run)


def fingerprint(payload: dict[str, str]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
