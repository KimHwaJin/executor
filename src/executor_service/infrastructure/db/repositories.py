"""SQLAlchemy repository and unit-of-work implementations."""

from types import TracebackType
from typing import Self
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from executor_service.domain.errors import PersistenceConflictError
from executor_service.domain.models import Execution, ExecutionStep, OutboxEvent
from executor_service.infrastructure.db.models import (
    CommandReceiptORM,
    ExecutionORM,
    ExecutionRetryORM,
    ExecutionStepORM,
    OutboxEventORM,
)


class SQLAlchemyExecutionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, execution: Execution) -> None:
        self._session.add(ExecutionORM.from_domain(execution))

    async def get(self, execution_id: UUID, *, for_update: bool = False) -> Execution | None:
        statement = (
            select(ExecutionORM)
            .where(ExecutionORM.id == execution_id)
            .options(selectinload(ExecutionORM.steps))
        )
        if for_update:
            statement = statement.with_for_update()
        row = await self._session.scalar(statement)
        return row.to_domain() if row else None

    async def get_by_submit_key(self, idempotency_key: str) -> Execution | None:
        row = await self._session.scalar(
            select(ExecutionORM)
            .where(ExecutionORM.idempotency_key == idempotency_key)
            .options(selectinload(ExecutionORM.steps))
        )
        return row.to_domain() if row else None

    async def get_by_cancel_key(self, idempotency_key: str) -> Execution | None:
        row = await self._session.scalar(
            select(ExecutionORM)
            .where(ExecutionORM.cancel_idempotency_key == idempotency_key)
            .options(selectinload(ExecutionORM.steps))
        )
        return row.to_domain() if row else None

    async def get_by_retry_key(self, idempotency_key: str) -> Execution | None:
        row = await self._session.scalar(
            select(ExecutionORM)
            .join(ExecutionRetryORM, ExecutionRetryORM.execution_id == ExecutionORM.id)
            .where(ExecutionRetryORM.idempotency_key == idempotency_key)
            .options(selectinload(ExecutionORM.steps))
        )
        return row.to_domain() if row else None

    async def add_retry_receipt(
        self, execution_id: UUID, idempotency_key: str, from_sequence: int
    ) -> None:
        self._session.add(
            ExecutionRetryORM(
                execution_id=execution_id,
                idempotency_key=idempotency_key,
                from_sequence=from_sequence,
            )
        )

    async def get_command_receipt(
        self, idempotency_key: str
    ) -> tuple[str, str, dict[str, object]] | None:
        receipt = await self._session.scalar(
            select(CommandReceiptORM).where(
                CommandReceiptORM.idempotency_key == idempotency_key
            )
        )
        if receipt is None:
            return None
        return receipt.command_type, receipt.request_fingerprint, receipt.result

    async def add_command_receipt(
        self,
        idempotency_key: str,
        command_type: str,
        request_fingerprint: str,
        result: dict[str, object],
    ) -> None:
        self._session.add(
            CommandReceiptORM(
                idempotency_key=idempotency_key,
                command_type=command_type,
                request_fingerprint=request_fingerprint,
                result=result,
            )
        )

    async def add_step(self, execution_id: UUID, step: ExecutionStep) -> None:
        row = ExecutionStepORM.from_domain(step)
        row.execution_id = execution_id
        self._session.add(row)

    async def save(self, execution: Execution) -> None:
        previous_version = execution.version - 1
        result = await self._session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution.id, ExecutionORM.version == previous_version)
            .values(
                status=execution.status,
                cancel_idempotency_key=execution.cancel_idempotency_key,
                cancellation_reason=execution.cancellation_reason,
                version=execution.version,
                updated_at=execution.updated_at,
                started_at=execution.started_at,
                finished_at=execution.finished_at,
                error_message=execution.error_message,
                failure_type=execution.failure_type,
                lease_owner=execution.lease_owner,
                lease_expires_at=execution.lease_expires_at,
                heartbeat_at=execution.heartbeat_at,
                retryable=execution.retryable,
                retry_strategy=execution.retry_strategy,
                retry_from_sequence=execution.retry_from_sequence,
                retained_kernel_until=execution.retained_kernel_until,
                retry_count=execution.retry_count,
                recovery_count=execution.recovery_count,
                kernel_cleanup_status=execution.kernel_cleanup_status,
                dynamic_finish_requested=execution.dynamic_finish_requested,
                dynamic_wait_expires_at=execution.dynamic_wait_expires_at,
                execution_expires_at=execution.execution_expires_at,
            )
        )
        if getattr(result, "rowcount", None) != 1:
            raise PersistenceConflictError("Execution was concurrently modified.")
        for step in execution.steps:
            await self._session.execute(
                update(ExecutionStepORM)
                .where(ExecutionStepORM.id == step.id)
                .values(
                    status=step.status,
                    code=step.code,
                    code_hash=step.code_hash,
                    plan_revision_id=step.plan_revision_id,
                    outputs=step.outputs,
                    error_message=step.error_message,
                    updated_at=step.updated_at,
                    started_at=step.started_at,
                    finished_at=step.finished_at,
                )
            )


class SQLAlchemyOutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, event: OutboxEvent) -> None:
        self._session.add(OutboxEventORM.from_domain(event))


class SQLAlchemyUnitOfWork:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> Self:
        self._session = self._session_factory()
        self.executions = SQLAlchemyExecutionRepository(self._session)
        self.outbox = SQLAlchemyOutboxRepository(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._session is None:
            return
        if self._session.in_transaction():
            await self._session.rollback()
        await self._session.close()

    async def commit(self) -> None:
        if self._session is None:
            raise RuntimeError("Unit of work was not entered.")
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise PersistenceConflictError("A persistence constraint was violated.") from exc

    async def rollback(self) -> None:
        if self._session is not None:
            await self._session.rollback()
