"""Fenced error-path persistence and cursor reads, separate from event traffic."""

import asyncio
import logging
from dataclasses import asdict
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.application.diagnostics import DiagnosticView
from executor_service.application.pagination import (
    Page,
    decode_time_cursor,
    encode_time_cursor,
)
from executor_service.domain.diagnostics import (
    DiagnosticCategory,
    DiagnosticCause,
    DiagnosticOrigin,
    RuntimeDiagnostic,
)
from executor_service.domain.enums import ExecutionStatus
from executor_service.domain.models import utc_now
from executor_service.infrastructure._execution_queries.guards import (
    require_execution,
)
from executor_service.infrastructure.db.models import (
    ExecutionDiagnosticORM,
    ExecutionStepORM,
)
from executor_service.infrastructure.diagnostic_mapping import diagnostic_for
from executor_service.infrastructure.execution_leases import (
    CancellationLease,
    ExecutionLease,
    ExecutionLeaseLostError,
    require_active_cancellation_lease,
    require_active_lease,
)
from executor_service.infrastructure.runtime_diagnostics import (
    log_runtime_failure,
)

logger = logging.getLogger(__name__)


class DiagnosticRecorder:
    """Never replace the initiating failure if diagnostic persistence fails."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def record(
        self,
        lease: ExecutionLease | CancellationLease,
        error: BaseException,
        *,
        phase: str,
        category: DiagnosticCategory,
        sequence: int | None = None,
        occurred_at: datetime | None = None,
    ) -> bool:
        observed = occurred_at or utc_now()
        detail = diagnostic_for(error, phase=phase, category=category)
        try:
            # Error paths only. No unbounded wait on an unavailable database.
            async with asyncio.timeout(2):
                async with self._factory() as session, session.begin():
                    if isinstance(lease, ExecutionLease):
                        execution, _ = await require_active_lease(
                            session,
                            lease,
                            allowed_statuses=(
                                ExecutionStatus.RUNNING,
                                ExecutionStatus.CANCEL_REQUESTED,
                            ),
                        )
                        attempt_id = lease.attempt_id
                    else:
                        execution = await require_active_cancellation_lease(
                            session, lease
                        )
                        attempt_id = None
                    step = None
                    if sequence is not None:
                        step = await session.scalar(
                            select(ExecutionStepORM).where(
                                ExecutionStepORM.execution_id == execution.id,
                                ExecutionStepORM.sequence == sequence,
                            )
                        )
                        if step is None:
                            raise ValueError("Diagnostic Step was not found.")
                    now = utc_now()
                    actor_type = execution.updated_by_type
                    actor_id = execution.updated_by
                    session.add(
                        ExecutionDiagnosticORM(
                            execution_id=execution.id,
                            attempt_id=attempt_id,
                            operation_id=step.operation_id
                            if step
                            else execution.active_operation_id,
                            step_id=step.id if step else None,
                            step_sequence=sequence,
                            fencing_token=lease.fencing_token,
                            detail=asdict(detail),
                            occurred_at=observed,
                            created_at=now,
                            updated_at=now,
                            created_by_type=actor_type,
                            created_by=actor_id,
                            updated_by_type=actor_type,
                            updated_by=actor_id,
                        )
                    )
            return True
        except ExecutionLeaseLostError:
            # Stale workers cannot append evidence to a new owner's history.
            return False
        except Exception as exc:
            log_runtime_failure(
                logger,
                error,
                phase=detail.phase,
                execution_id=lease.execution_id,
                fencing_token=lease.fencing_token,
                diagnostic_code=detail.code,
            )
            log_runtime_failure(
                logger,
                exc,
                phase="DIAGNOSTIC_PERSIST",
                execution_id=lease.execution_id,
                fencing_token=lease.fencing_token,
                diagnostic_code=detail.code,
                diagnostic_phase=detail.phase,
            )
            return False


class SQLAlchemyDiagnosticQueryService:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def list(
        self,
        execution_id: UUID,
        *,
        attempt_id: UUID | None = None,
        operation_id: UUID | None = None,
        step_id: UUID | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[DiagnosticView]:
        if not 1 <= limit <= 200:
            raise ValueError(
                "Diagnostic page limit must be between 1 and 200."
            )
        kind = (
            f"diagnostics:{execution_id}:{attempt_id}:{operation_id}:{step_id}"
        )
        stmt = select(ExecutionDiagnosticORM).where(
            ExecutionDiagnosticORM.execution_id == execution_id
        )
        for column, value in (
            (ExecutionDiagnosticORM.attempt_id, attempt_id),
            (ExecutionDiagnosticORM.operation_id, operation_id),
            (ExecutionDiagnosticORM.step_id, step_id),
        ):
            if value is not None:
                stmt = stmt.where(column == value)
        if cursor is not None:
            created_at, item_id = decode_time_cursor(cursor, kind)
            stmt = stmt.where(
                or_(
                    ExecutionDiagnosticORM.created_at > created_at,
                    and_(
                        ExecutionDiagnosticORM.created_at == created_at,
                        ExecutionDiagnosticORM.id > item_id,
                    ),
                )
            )
        async with self._factory() as session:
            await require_execution(session, execution_id)
            rows = list(
                await session.scalars(
                    stmt.order_by(
                        ExecutionDiagnosticORM.created_at,
                        ExecutionDiagnosticORM.id,
                    ).limit(limit + 1)
                )
            )
        page = rows[:limit]
        return Page(
            items=[_view(row) for row in page],
            next_cursor=encode_time_cursor(
                kind, page[-1].created_at, page[-1].id
            )
            if len(rows) > limit
            else None,
        )


def _view(row: ExecutionDiagnosticORM) -> DiagnosticView:
    data = row.detail
    diagnostic = RuntimeDiagnostic(
        code=data["code"],
        phase=data["phase"],
        category=DiagnosticCategory(data["category"]),
        origin=DiagnosticOrigin(data["origin"]),
        message=data["message"],
        causes=tuple(DiagnosticCause(**cause) for cause in data["causes"]),
        causes_truncated=data["causes_truncated"],
    )
    return DiagnosticView(
        id=row.id,
        execution_id=row.execution_id,
        attempt_id=row.attempt_id,
        operation_id=row.operation_id,
        step_id=row.step_id,
        step_sequence=row.step_sequence,
        fencing_token=row.fencing_token,
        diagnostic=diagnostic,
        occurred_at=row.occurred_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        created_by_type=row.created_by_type,
        created_by=row.created_by,
        updated_by_type=row.updated_by_type,
        updated_by=row.updated_by,
    )
