"""SQLAlchemy repository and unit-of-work implementations."""

from types import TracebackType
from typing import Self
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from executor_service.domain.enums import ActorType, OperationStatus
from executor_service.domain.errors import PersistenceConflictError
from executor_service.domain.models import (
    Execution,
    ExecutionOperation,
    ExecutionStep,
    OutboxEvent,
    utc_now,
)
from executor_service.infrastructure.db.models import (
    CommandReceiptORM,
    ExecutionOperationORM,
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

    async def get(
        self, execution_id: UUID, *, for_update: bool = False
    ) -> Execution | None:
        statement = (
            select(ExecutionORM)
            .where(ExecutionORM.id == execution_id)
            .options(selectinload(ExecutionORM.steps))
        )
        if for_update:
            statement = statement.with_for_update()
        row = await self._session.scalar(statement)
        return row.to_domain() if row else None

    async def get_by_submit_key(
        self, idempotency_key: str
    ) -> Execution | None:
        row = await self._session.scalar(
            select(ExecutionORM)
            .where(ExecutionORM.idempotency_key == idempotency_key)
            .options(selectinload(ExecutionORM.steps))
        )
        return row.to_domain() if row else None

    async def get_by_cancel_key(
        self, idempotency_key: str
    ) -> Execution | None:
        row = await self._session.scalar(
            select(ExecutionORM)
            .where(ExecutionORM.cancel_idempotency_key == idempotency_key)
            .options(selectinload(ExecutionORM.steps))
        )
        return row.to_domain() if row else None

    async def get_by_retry_key(self, idempotency_key: str) -> Execution | None:
        row = await self._session.scalar(
            select(ExecutionORM)
            .join(
                ExecutionRetryORM,
                ExecutionRetryORM.execution_id == ExecutionORM.id,
            )
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
        return (
            receipt.command_type,
            receipt.request_fingerprint,
            receipt.result,
        )

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

    async def add_operation(self, operation: ExecutionOperation) -> None:
        self._session.add(ExecutionOperationORM.from_domain(operation))
        # A follow-up MULTI Operation and its Steps are independent ORM
        # instances. Flush the parent before the caller adds child Steps so
        # PostgreSQL never observes an execution_steps.operation_id whose
        # Operation has not been inserted yet.
        await self._session.flush()

    async def next_operation_number(self, execution_id: UUID) -> int:
        current = await self._session.scalar(
            select(func.max(ExecutionOperationORM.operation_number)).where(
                ExecutionOperationORM.execution_id == execution_id
            )
        )
        return (current or 0) + 1

    async def get_operation_id_by_key(
        self, idempotency_key: str
    ) -> UUID | None:
        return await self._session.scalar(
            select(ExecutionOperationORM.id).where(
                ExecutionOperationORM.idempotency_key == idempotency_key
            )
        )

    async def requeue_operation_for_retry(
        self,
        operation_id: UUID,
        *,
        updated_by_type: ActorType | None,
        updated_by: str | None,
    ) -> None:
        result = await self._session.execute(
            update(ExecutionOperationORM)
            .where(
                ExecutionOperationORM.id == operation_id,
                ExecutionOperationORM.status == OperationStatus.FAILED,
            )
            .values(
                status=OperationStatus.QUEUED,
                execution_attempt_id=None,
                error_message=None,
                updated_by_type=updated_by_type,
                updated_by=updated_by,
                updated_at=utc_now(),
                started_at=None,
                finished_at=None,
            )
        )
        if getattr(result, "rowcount", None) != 1:
            raise PersistenceConflictError(
                "Active Operation is not retryable from FAILED."
            )

    async def save(self, execution: Execution) -> None:
        previous_version = execution.version - 1
        result = await self._session.execute(
            update(ExecutionORM)
            .where(
                ExecutionORM.id == execution.id,
                ExecutionORM.version == previous_version,
            )
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
                retry_strategy=execution.retry_strategy,
                retry_from_sequence=execution.retry_from_sequence,
                retained_runtime_session_until=execution.retained_runtime_session_until,
                retry_count=execution.retry_count,
                recovery_count=execution.recovery_count,
                runtime_session_cleanup_status=execution.runtime_session_cleanup_status,
                finalization_requested=execution.finalization_requested,
                active_operation_id=execution.active_operation_id,
                operation_wait_expires_at=execution.operation_wait_expires_at,
                execution_expires_at=execution.execution_expires_at,
                traceparent=execution.traceparent,
                tracestate=execution.tracestate,
                updated_by_type=execution.updated_by_type,
                updated_by=execution.updated_by,
            )
        )
        if getattr(result, "rowcount", None) != 1:
            raise PersistenceConflictError(
                "Execution was concurrently modified."
            )
        for step in execution.steps:
            await self._session.execute(
                update(ExecutionStepORM)
                .where(ExecutionStepORM.id == step.id)
                .values(
                    status=step.status,
                    source_type=step.source_type,
                    source_path=step.source_path,
                    source_sha256=step.source_sha256,
                    source_snapshot_path=step.source_snapshot_path,
                    source_size_bytes=step.source_size_bytes,
                    code_hash=step.code_hash,
                    step_timeout_seconds=step.step_timeout_seconds,
                    updated_by_type=step.updated_by_type,
                    updated_by=step.updated_by,
                    output_summary=step.output_summary,
                    result_execution_attempt_id=(
                        step.result_execution_attempt_id
                    ),
                    result_manifest_path=step.result_manifest_path,
                    result_manifest_checksum_sha256=(
                        step.result_manifest_checksum_sha256
                    ),
                    result_fencing_token=step.result_fencing_token,
                    result_complete=step.result_complete,
                    result_representation_count=(
                        step.result_representation_count
                    ),
                    result_total_size_bytes=(step.result_total_size_bytes),
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
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
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
            raise PersistenceConflictError(
                "A persistence constraint was violated."
            ) from exc

    async def rollback(self) -> None:
        if self._session is not None:
            await self._session.rollback()
