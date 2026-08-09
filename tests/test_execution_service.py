from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.commands import (
    CancelExecutionCommand,
    RetryExecutionCommand,
    StepSpec,
    SubmitExecutionCommand,
)
from executor_service.application.services import ExecutionService
from executor_service.domain.enums import (
    CodeSourceType,
    ExecutionMode,
    ExecutionStatus,
    FailureType,
    JupyterPool,
    KernelCleanupStatus,
    RetryStrategy,
    StepStatus,
    TriggerType,
)
from executor_service.domain.errors import ExecutionNotFoundError, IdempotencyConflictError
from executor_service.domain.models import utc_now
from executor_service.infrastructure.db.models import ExecutionORM, ExecutionStepORM
from executor_service.infrastructure.db.session import create_session_factory


def submit_command(idempotency_key: str = "submit-1") -> SubmitExecutionCommand:
    return SubmitExecutionCommand(
        idempotency_key=idempotency_key,
        mode=ExecutionMode.STATIC,
        trigger_type=TriggerType.INTERACTIVE,
        jupyter_pool=JupyterPool.INTERACTIVE,
        kernel_name="python-analysis-a",
        code_source_type=CodeSourceType.INLINE,
        code="print('hello')",
        code_path=None,
        requested_by_user_id="user-1",
        project_id="project-1",
        session_id="session-1",
        execution_plan_id="plan-1",
        steps=(StepSpec(sequence=0, skill_name="data_load", tool_name="load_data"),),
    )


async def test_submit_get_cancel_and_idempotency(execution_service: ExecutionService) -> None:
    submitted = await execution_service.submit(submit_command())
    duplicate = await execution_service.submit(submit_command())

    assert duplicate.id == submitted.id
    assert submitted.status == ExecutionStatus.QUEUED
    assert submitted.steps[0].tool_name == "load_data"

    loaded = await execution_service.get(submitted.id)
    assert loaded.id == submitted.id

    cancelled = await execution_service.cancel(
        CancelExecutionCommand(
            execution_id=submitted.id,
            idempotency_key="cancel-1",
            reason="user requested",
        )
    )
    assert cancelled.status == ExecutionStatus.CANCEL_REQUESTED
    assert cancelled.version == 1

    repeated_cancel = await execution_service.cancel(
        CancelExecutionCommand(
            execution_id=submitted.id,
            idempotency_key="cancel-1",
            reason="user requested",
        )
    )
    assert repeated_cancel.status == ExecutionStatus.CANCEL_REQUESTED
    assert repeated_cancel.version == 1


async def test_submit_key_rejects_different_request(
    execution_service: ExecutionService,
) -> None:
    await execution_service.submit(submit_command())
    changed = replace(submit_command(), code="print('changed')")

    with pytest.raises(IdempotencyConflictError):
        await execution_service.submit(changed)


async def test_unknown_execution_is_not_found(execution_service: ExecutionService) -> None:
    with pytest.raises(ExecutionNotFoundError, match="was not found"):
        await execution_service.get(uuid4())


async def test_retry_resets_failed_and_later_steps_idempotently(
    execution_service: ExecutionService,
    engine: AsyncEngine,
) -> None:
    execution = await execution_service.submit(
        replace(
            submit_command("retry-submit"),
            steps=(
                StepSpec(sequence=0, tool_name="prepare"),
                StepSpec(sequence=1, tool_name="fail_once"),
                StepSpec(sequence=2, tool_name="finish"),
            ),
        )
    )
    now = utc_now()
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution.id)
            .values(
                status=ExecutionStatus.FAILED,
                retryable=True,
                retry_strategy=RetryStrategy.FROM_FAILED_STEP,
                retry_from_sequence=1,
                retained_kernel_until=now + timedelta(hours=1),
                kernel_id="retained-kernel",
                jupyter_server_id=uuid4(),
                finished_at=now,
            )
        )
        await session.execute(
            update(ExecutionStepORM)
            .where(ExecutionStepORM.execution_id == execution.id)
            .values(status=StepStatus.SKIPPED, finished_at=now)
        )
        await session.execute(
            update(ExecutionStepORM)
            .where(
                ExecutionStepORM.execution_id == execution.id,
                ExecutionStepORM.sequence == 0,
            )
            .values(status=StepStatus.SUCCEEDED)
        )
        await session.execute(
            update(ExecutionStepORM)
            .where(
                ExecutionStepORM.execution_id == execution.id,
                ExecutionStepORM.sequence == 1,
            )
            .values(status=StepStatus.FAILED, error_message="expected failure")
        )

    command = RetryExecutionCommand(
        execution_id=execution.id, idempotency_key="retry-command"
    )
    retried = await execution_service.retry(command)
    repeated = await execution_service.retry(command)

    assert retried.status == ExecutionStatus.QUEUED
    assert retried.retry_count == 1
    assert retried.steps[0].status == StepStatus.SUCCEEDED
    assert [step.status for step in retried.steps[1:]] == [
        StepStatus.PENDING,
        StepStatus.PENDING,
    ]
    assert repeated.id == retried.id
    assert repeated.retry_count == 1


async def test_infrastructure_retry_starts_from_zero_with_a_new_kernel(
    execution_service: ExecutionService,
    engine: AsyncEngine,
) -> None:
    execution = await execution_service.submit(
        replace(
            submit_command("infrastructure-retry-submit"),
            steps=(
                StepSpec(sequence=0, tool_name="prepare"),
                StepSpec(sequence=1, tool_name="long_running_tool"),
            ),
        )
    )
    now = utc_now()
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution.id)
            .values(
                status=ExecutionStatus.FAILED,
                error_message="worker lease expired",
                failure_type=FailureType.LEASE_EXPIRED,
                retryable=True,
                retry_strategy=RetryStrategy.FROM_START,
                retry_from_sequence=0,
                kernel_id="abandoned-kernel",
                jupyter_server_id=uuid4(),
                kernel_cleanup_status=KernelCleanupStatus.FAILED,
                finished_at=now,
            )
        )
        await session.execute(
            update(ExecutionStepORM)
            .where(ExecutionStepORM.execution_id == execution.id)
            .values(status=StepStatus.FAILED, finished_at=now)
        )

    retried = await execution_service.retry(
        RetryExecutionCommand(
            execution_id=execution.id,
            idempotency_key="infrastructure-retry-command",
        )
    )

    assert retried.status == ExecutionStatus.QUEUED
    assert retried.retry_strategy == RetryStrategy.FROM_START
    assert retried.retry_from_sequence == 0
    assert retried.kernel_id is None
    assert retried.jupyter_server_id is None
    assert retried.kernel_cleanup_status == KernelCleanupStatus.NOT_REQUIRED
    assert [step.status for step in retried.steps] == [
        StepStatus.PENDING,
        StepStatus.PENDING,
    ]
