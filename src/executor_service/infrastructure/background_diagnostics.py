"""Version-checked, bounded observations outside an active Worker lease."""

import asyncio
import hashlib
import json
import logging
from collections import OrderedDict
from dataclasses import asdict, dataclass
from datetime import timedelta
from time import monotonic
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.domain.diagnostics import DiagnosticCategory
from executor_service.domain.enums import ExecutionStatus
from executor_service.domain.models import utc_now
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionDiagnosticORM,
    ExecutionORM,
)
from executor_service.infrastructure.diagnostic_mapping import diagnostic_for
from executor_service.infrastructure.runtime_diagnostics import (
    log_runtime_failure,
)

logger = logging.getLogger(__name__)
BACKGROUND_DIAGNOSTIC_INTERVAL_SECONDS = 300
_CACHE_LIMIT = 1024


@dataclass(frozen=True, slots=True)
class RuntimeObservation:
    """Internal optimistic token; never supplied by a caller/API."""

    execution_id: UUID
    status: ExecutionStatus
    version: int
    fencing_token: int
    operation_id: UUID | None
    target_id: UUID | None
    session_id: str | None

    @classmethod
    def capture(cls, row: ExecutionORM) -> "RuntimeObservation":
        return cls(
            row.id,
            row.status,
            row.version,
            row.fencing_token,
            row.active_operation_id,
            row.runtime_target_id,
            row.runtime_session_id,
        )

    async def current(
        self, session: AsyncSession, *, lock: bool = False
    ) -> ExecutionORM | None:
        statement = select(ExecutionORM).where(
            ExecutionORM.id == self.execution_id,
            ExecutionORM.status == self.status,
            ExecutionORM.version == self.version,
            ExecutionORM.fencing_token == self.fencing_token,
            ExecutionORM.active_operation_id == self.operation_id,
            ExecutionORM.runtime_target_id == self.target_id,
            ExecutionORM.runtime_session_id == self.session_id,
        )
        if lock:
            statement = statement.with_for_update()
        return await session.scalar(statement)


class BackgroundDiagnosticRecorder:
    """Rate limit observations, not Runtime probes or state transitions.

    Execution row locking serializes duplicate checks across processes. A small
    local TTL cache avoids repeated queries/log storms, including DB outages.
    Observations do not update Execution version, timestamps or events.
    """

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory
        self._recent: OrderedDict[tuple[object, ...], float] = OrderedDict()

    def _remember(self, key: tuple[object, ...]) -> None:
        self._recent[key] = (
            monotonic() + BACKGROUND_DIAGNOSTIC_INTERVAL_SECONDS
        )
        self._recent.move_to_end(key)
        while len(self._recent) > _CACHE_LIMIT:
            self._recent.popitem(last=False)

    def log_loop_failure(self, error: BaseException, *, phase: str) -> None:
        """A failed batch query has no trustworthy Execution scope to persist."""
        detail = diagnostic_for(
            error, phase=phase, category=DiagnosticCategory.EXECUTION
        )
        signature = hashlib.sha256(
            json.dumps(asdict(detail), sort_keys=True).encode()
        ).hexdigest()
        key = ("background-loop", phase, signature)
        if self._recent.get(key, 0) > monotonic():
            return
        self._remember(key)
        log_runtime_failure(logger, error, phase=phase)

    async def record(
        self,
        observation: RuntimeObservation,
        error: BaseException,
        *,
        phase: str,
        category: DiagnosticCategory,
    ) -> bool:
        detail = diagnostic_for(error, phase=phase, category=category)
        data = asdict(detail)
        signature = hashlib.sha256(
            json.dumps(data, sort_keys=True).encode()
        ).hexdigest()
        key = (
            observation.execution_id,
            observation.fencing_token,
            observation.operation_id,
            observation.target_id,
            observation.session_id,
            phase,
            signature,
        )
        if self._recent.get(key, 0) > monotonic():
            return False
        try:
            async with asyncio.timeout(2):
                async with self._factory() as session, session.begin():
                    row = await observation.current(session, lock=True)
                    if row is None or row.status not in {
                        ExecutionStatus.FAILED,
                        ExecutionStatus.CANCELLED,
                        ExecutionStatus.WAITING_FOR_OPERATION,
                    }:
                        return False
                    if observation.fencing_token < 1:
                        raise ValueError(
                            "Runtime observation has no execution fence."
                        )
                    now = utc_now()
                    recent = await session.scalars(
                        select(ExecutionDiagnosticORM.detail)
                        .where(
                            ExecutionDiagnosticORM.execution_id == row.id,
                            ExecutionDiagnosticORM.fencing_token
                            == observation.fencing_token,
                            ExecutionDiagnosticORM.operation_id
                            == observation.operation_id,
                            ExecutionDiagnosticORM.created_at
                            >= now
                            - timedelta(
                                seconds=BACKGROUND_DIAGNOSTIC_INTERVAL_SECONDS
                            ),
                            ExecutionDiagnosticORM.detail["phase"].as_string()
                            == phase,
                        )
                        .order_by(ExecutionDiagnosticORM.created_at.desc())
                        .limit(128)
                    )
                    stored_shape = json.loads(json.dumps(data))
                    if any(value == stored_shape for value in recent):
                        self._remember(key)
                        return False
                    attempt_id = await session.scalar(
                        select(ExecutionAttemptORM.id)
                        .where(ExecutionAttemptORM.execution_id == row.id)
                        .order_by(ExecutionAttemptORM.attempt_number.desc())
                        .limit(1)
                    )
                    session.add(
                        ExecutionDiagnosticORM(
                            execution_id=row.id,
                            attempt_id=attempt_id,
                            operation_id=observation.operation_id,
                            fencing_token=observation.fencing_token,
                            detail=data,
                            occurred_at=now,
                            created_at=now,
                            updated_at=now,
                            created_by_type=row.updated_by_type,
                            created_by=row.updated_by,
                            updated_by_type=row.updated_by_type,
                            updated_by=row.updated_by,
                        )
                    )
            self._remember(key)
            _log(error, phase, observation)
            return True
        except Exception as exc:
            self._remember(key)
            _log(error, phase, observation)
            _log(exc, "DIAGNOSTIC_PERSIST", observation)
            return False


def _log(
    error: BaseException, phase: str, observation: RuntimeObservation
) -> None:
    log_runtime_failure(
        logger,
        error,
        phase=phase,
        execution_id=observation.execution_id,
        fencing_token=observation.fencing_token,
        operation_id=observation.operation_id,
        target_id=observation.target_id,
        runtime_session_id=observation.session_id,
    )
