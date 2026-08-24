from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.commands import (
    CancelExecutionCommand,
    CreateOperationCommand,
    FinalizeExecutionCommand,
    RetryExecutionCommand,
    StepSpec,
    SubmitExecutionCommand,
)
from executor_service.application.services import ExecutionService
from executor_service.domain.enums import (
    ActorType,
    ExecutionStatus,
    FailureType,
    OperationMode,
    OperationStatus,
    RetryStrategy,
    RuntimeSessionCleanupStatus,
    StepStatus,
    TriggerType,
)
from executor_service.domain.errors import (
    ExecutionNotFoundError,
    ExecutionVersionConflictError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    UnsupportedRuntimeProfileError,
)
from executor_service.domain.models import utc_now
from executor_service.infrastructure.db.models import (
    ExecutionOperationORM,
    ExecutionORM,
    ExecutionStepORM,
)
from executor_service.infrastructure.db.session import create_session_factory


def submit_command(
    idempotency_key: str = "submit-1",
) -> SubmitExecutionCommand:
    return SubmitExecutionCommand(
        idempotency_key=idempotency_key,
        operation_mode=OperationMode.SINGLE,
        trigger_type=TriggerType.INTERACTIVE,
        runtime_profile="basic",
        user_id="user-1",
        project_id="project-1",
        session_id="session-1",
        task_id="test-task",
        steps=(
            StepSpec(
                sequence=0,
                code="print('hello')",
                skill_name="data_load",
                tool_name="load_data",
            ),
        ),
    )


def multi_submit_command(
    idempotency_key: str = "multi-submit-1",
) -> SubmitExecutionCommand:
    code = "value = 40"
    return replace(
        submit_command(idempotency_key),
        operation_mode=OperationMode.MULTI,
        operation_wait_timeout_seconds=3600,
        steps=(
            StepSpec(
                sequence=0,
                code=code,
                skill_name="data_load",
                tool_name="load_data",
            ),
        ),
    )


async def test_submit_rejects_unconfigured_runtime_profile(
    execution_service: ExecutionService,
) -> None:
    with pytest.raises(UnsupportedRuntimeProfileError):
        await execution_service.submit(
            replace(
                submit_command("unsupported-profile"),
                runtime_profile="unknown",
            )
        )


async def test_submit_get_cancel_and_idempotency(
    execution_service: ExecutionService,
) -> None:
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
    changed = replace(
        submit_command(),
        steps=(StepSpec(sequence=0, code="print('changed')"),),
    )

    with pytest.raises(IdempotencyConflictError):
        await execution_service.submit(changed)


async def test_interactive_submit_requires_actor_to_match_user(
    execution_service: ExecutionService,
) -> None:
    command = replace(
        submit_command("interactive-actor-mismatch"),
        actor_type=ActorType.USER,
        actor_id="another-user",
    )

    with pytest.raises(
        InvalidStateTransitionError,
        match=r"actor\.id to match context\.user_id",
    ):
        await execution_service.submit(command)


async def test_batch_submit_allows_actor_to_differ_from_owning_user(
    execution_service: ExecutionService,
) -> None:
    command = replace(
        submit_command("batch-actor-differs"),
        trigger_type=TriggerType.BATCH,
        actor_type=ActorType.BATCH,
        actor_id="schedule-1",
        workflow_id="workflow-1",
    )

    submitted = await execution_service.submit(command)

    assert submitted.user_id == "user-1"
    assert submitted.runtime_pool.value == "BATCH"
    assert submitted.created_by_type == ActorType.BATCH
    assert submitted.created_by == "schedule-1"


async def test_unknown_execution_is_not_found(
    execution_service: ExecutionService,
) -> None:
    with pytest.raises(ExecutionNotFoundError, match="was not found"):
        await execution_service.get(uuid4())


async def test_multi_continue_and_finish_are_versioned_and_idempotent(
    execution_service: ExecutionService,
    engine: AsyncEngine,
) -> None:
    execution = await execution_service.submit(multi_submit_command())
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution.id)
            .values(status=ExecutionStatus.WAITING_FOR_OPERATION, version=2)
        )

    command = CreateOperationCommand(
        execution_id=execution.id,
        idempotency_key="multi-continue-1",
        expected_version=2,
        steps=(
            StepSpec(
                sequence=1,
                code="answer = value + 2",
                skill_name="eda",
                tool_name="calculate_answer",
            ),
            StepSpec(
                sequence=2,
                code="print(answer)",
            ),
        ),
    )
    continued = await execution_service.create_operation(command)
    repeated = await execution_service.create_operation(command)

    assert continued.status == ExecutionStatus.QUEUED
    assert continued.version == 3
    assert len(continued.steps) == 3
    assert continued.steps[1].code_hash is not None
    assert repeated.version == 3
    assert len(repeated.steps) == 3

    async with session_factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution.id)
            .values(status=ExecutionStatus.WAITING_FOR_OPERATION, version=4)
        )

    finish = FinalizeExecutionCommand(
        execution_id=execution.id,
        idempotency_key="multi-finish-1",
        expected_version=4,
    )
    finishing = await execution_service.finalize_execution(finish)
    repeated_finish = await execution_service.finalize_execution(finish)
    assert finishing.status == ExecutionStatus.FINALIZING
    assert finishing.finalization_requested
    assert repeated_finish.version == 5


async def test_multi_continue_rejects_stale_version(
    execution_service: ExecutionService,
    engine: AsyncEngine,
) -> None:
    execution = await execution_service.submit(
        multi_submit_command("multi-stale")
    )
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution.id)
            .values(status=ExecutionStatus.WAITING_FOR_OPERATION, version=2)
        )

    with pytest.raises(ExecutionVersionConflictError):
        await execution_service.create_operation(
            CreateOperationCommand(
                execution_id=execution.id,
                idempotency_key="multi-stale-continue",
                expected_version=1,
                steps=(
                    StepSpec(
                        sequence=1,
                        code="print('stale')",
                    ),
                ),
            )
        )


async def test_retry_resets_failed_and_later_steps_idempotently(
    execution_service: ExecutionService,
    engine: AsyncEngine,
) -> None:
    execution = await execution_service.submit(
        replace(
            submit_command("retry-submit"),
            steps=(
                StepSpec(0, "prepare()", tool_name="prepare"),
                StepSpec(1, "fail_once()", tool_name="fail_once"),
                StepSpec(2, "finish()", tool_name="finish"),
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
                retry_strategy=RetryStrategy.FROM_FAILED_STEP,
                retry_from_sequence=1,
                retained_runtime_session_until=now + timedelta(hours=1),
                runtime_session_id="retained-kernel",
                runtime_target_id=uuid4(),
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
        await session.execute(
            update(ExecutionOperationORM)
            .where(ExecutionOperationORM.id == execution.active_operation_id)
            .values(status=OperationStatus.FAILED, finished_at=now)
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
    assert retried.active_operation_id == execution.active_operation_id
    async with session_factory() as session:
        operation = await session.get(
            ExecutionOperationORM, execution.active_operation_id
        )
    assert operation is not None
    assert operation.status == OperationStatus.QUEUED
    assert operation.execution_attempt_id is None
    assert operation.finished_at is None


async def test_infrastructure_retry_starts_from_zero_with_a_new_kernel(
    execution_service: ExecutionService,
    engine: AsyncEngine,
) -> None:
    execution = await execution_service.submit(
        replace(
            submit_command("infrastructure-retry-submit"),
            steps=(
                StepSpec(0, "prepare()", tool_name="prepare"),
                StepSpec(
                    1,
                    "long_running_tool()",
                    tool_name="long_running_tool",
                ),
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
                retry_strategy=RetryStrategy.FROM_START,
                retry_from_sequence=0,
                runtime_session_id="abandoned-kernel",
                runtime_target_id=uuid4(),
                runtime_session_cleanup_status=RuntimeSessionCleanupStatus.FAILED,
                finished_at=now,
            )
        )
        await session.execute(
            update(ExecutionStepORM)
            .where(ExecutionStepORM.execution_id == execution.id)
            .values(status=StepStatus.FAILED, finished_at=now)
        )
        await session.execute(
            update(ExecutionOperationORM)
            .where(ExecutionOperationORM.id == execution.active_operation_id)
            .values(status=OperationStatus.FAILED, finished_at=now)
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
    assert retried.runtime_session_id is None
    assert retried.runtime_target_id is None
    assert (
        retried.runtime_session_cleanup_status
        == RuntimeSessionCleanupStatus.NOT_REQUIRED
    )
    assert [step.status for step in retried.steps] == [
        StepStatus.PENDING,
        StepStatus.PENDING,
    ]


async def test_infrastructure_retry_waits_for_abandoned_runtime_session_cleanup(
    execution_service: ExecutionService,
    engine: AsyncEngine,
) -> None:
    execution = await execution_service.submit(
        submit_command("cleanup-pending-retry-submit")
    )
    now = utc_now()
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution.id)
            .values(
                status=ExecutionStatus.FAILED,
                failure_type=FailureType.LEASE_EXPIRED,
                retry_strategy=RetryStrategy.FROM_START,
                retry_from_sequence=0,
                runtime_session_id="cleanup-pending-kernel",
                runtime_target_id=uuid4(),
                runtime_session_cleanup_status=RuntimeSessionCleanupStatus.PENDING,
                finished_at=now,
            )
        )

    with pytest.raises(InvalidStateTransitionError, match="still cleaning up"):
        await execution_service.retry(
            RetryExecutionCommand(
                execution_id=execution.id,
                idempotency_key="cleanup-pending-retry-command",
            )
        )

    unchanged = await execution_service.get(execution.id)
    assert unchanged.status == ExecutionStatus.FAILED
    assert (
        unchanged.runtime_session_cleanup_status
        == RuntimeSessionCleanupStatus.PENDING
    )


async def test_multi_execution_rejects_explicit_retry(
    execution_service: ExecutionService,
    engine: AsyncEngine,
) -> None:
    execution = await execution_service.submit(
        replace(
            submit_command("multi-explicit-retry-submit"),
            operation_mode=OperationMode.MULTI,
            operation_wait_timeout_seconds=3600,
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
                retry_strategy=RetryStrategy.FROM_START,
                retry_from_sequence=0,
                finished_at=now,
            )
        )
        await session.execute(
            update(ExecutionOperationORM)
            .where(ExecutionOperationORM.id == execution.active_operation_id)
            .values(status=OperationStatus.FAILED, finished_at=now)
        )

    with pytest.raises(InvalidStateTransitionError, match="Only SINGLE"):
        await execution_service.retry(
            RetryExecutionCommand(
                execution_id=execution.id,
                idempotency_key="multi-explicit-retry-command",
            )
        )
