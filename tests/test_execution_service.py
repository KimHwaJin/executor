from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.commands import (
    CancelExecutionCommand,
    ContinueExecutionCommand,
    FinishExecutionCommand,
    RetryExecutionCommand,
    StepSpec,
    SubmitExecutionCommand,
)
from executor_service.application.services import ExecutionService
from executor_service.domain.enums import (
    ActorType,
    CodeSourceType,
    ExecutionMode,
    ExecutionStatus,
    FailureType,
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
)
from executor_service.domain.models import utc_now
from executor_service.infrastructure.db.models import ExecutionORM, ExecutionStepORM
from executor_service.infrastructure.db.session import create_session_factory


def submit_command(idempotency_key: str = "submit-1") -> SubmitExecutionCommand:
    return SubmitExecutionCommand(
        idempotency_key=idempotency_key,
        mode=ExecutionMode.STATIC,
        trigger_type=TriggerType.INTERACTIVE,
        runtime_profile="python-analysis-a",
        code_source_type=CodeSourceType.INLINE,
        source_content="print('hello')",
        code_path=None,
        source_sha256="0" * 64,
        user_id="user-1",
        project_id="project-1",
        session_id="session-1",
        task_id="test-task",
        execution_plan_id="plan-1",
        steps=(
            StepSpec(
                sequence=0,
                code="print('hello')",
                execution_plan_id="plan-1",
                plan_step_id="plan-1-step-0",
                skill_name="data_load",
                tool_name="load_data",
            ),
        ),
    )


def dynamic_submit_command(idempotency_key: str = "dynamic-submit-1") -> SubmitExecutionCommand:
    code = "value = 40"
    return replace(
        submit_command(idempotency_key),
        mode=ExecutionMode.DYNAMIC,
        execution_plan_id="plan-revision-1",
        source_content=code,
        steps=(
            StepSpec(
                sequence=0,
                code=code,
                execution_plan_id="plan-revision-1",
                plan_step_id="plan-revision-1-step-0",
                skill_name="data_load",
                tool_name="load_data",
            ),
        ),
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
    changed = replace(submit_command(), source_content="print('changed')")

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
    )

    submitted = await execution_service.submit(command)

    assert submitted.user_id == "user-1"
    assert submitted.runtime_pool.value == "BATCH"
    assert submitted.created_by_type == ActorType.BATCH
    assert submitted.created_by == "schedule-1"


async def test_unknown_execution_is_not_found(execution_service: ExecutionService) -> None:
    with pytest.raises(ExecutionNotFoundError, match="was not found"):
        await execution_service.get(uuid4())


async def test_dynamic_continue_and_finish_are_versioned_and_idempotent(
    execution_service: ExecutionService,
    engine: AsyncEngine,
) -> None:
    execution = await execution_service.submit(dynamic_submit_command())
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution.id)
            .values(status=ExecutionStatus.WAITING_FOR_NEXT_STEP, version=2)
        )

    command = ContinueExecutionCommand(
        execution_id=execution.id,
        idempotency_key="dynamic-continue-1",
        expected_version=2,
        step=StepSpec(
            sequence=1,
            code="answer = value + 2",
            execution_plan_id="plan-revision-2",
            plan_step_id="plan-revision-2-step-1",
            skill_name="eda",
            tool_name="calculate_answer",
        ),
    )
    continued = await execution_service.continue_execution(command)
    repeated = await execution_service.continue_execution(command)

    assert continued.status == ExecutionStatus.QUEUED
    assert continued.version == 3
    assert len(continued.steps) == 2
    assert continued.steps[1].code_hash is not None
    assert repeated.version == 3
    assert len(repeated.steps) == 2

    async with session_factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution.id)
            .values(status=ExecutionStatus.WAITING_FOR_NEXT_STEP, version=4)
        )

    finish = FinishExecutionCommand(
        execution_id=execution.id,
        idempotency_key="dynamic-finish-1",
        expected_version=4,
    )
    finishing = await execution_service.finish_execution(finish)
    repeated_finish = await execution_service.finish_execution(finish)
    assert finishing.status == ExecutionStatus.QUEUED
    assert finishing.dynamic_finish_requested
    assert repeated_finish.version == 5


async def test_dynamic_continue_rejects_stale_version(
    execution_service: ExecutionService,
    engine: AsyncEngine,
) -> None:
    execution = await execution_service.submit(dynamic_submit_command("dynamic-stale"))
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution.id)
            .values(status=ExecutionStatus.WAITING_FOR_NEXT_STEP, version=2)
        )

    with pytest.raises(ExecutionVersionConflictError):
        await execution_service.continue_execution(
            ContinueExecutionCommand(
                execution_id=execution.id,
                idempotency_key="dynamic-stale-continue",
                expected_version=1,
                step=StepSpec(
                    sequence=1,
                    code="print('stale')",
                    execution_plan_id="plan-revision-stale",
                    plan_step_id="plan-revision-stale-step-1",
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
                StepSpec(0, "prepare()", "plan-1", "plan-1-step-0", tool_name="prepare"),
                StepSpec(1, "fail_once()", "plan-1", "plan-1-step-1", tool_name="fail_once"),
                StepSpec(2, "finish()", "plan-1", "plan-1-step-2", tool_name="finish"),
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

    command = RetryExecutionCommand(execution_id=execution.id, idempotency_key="retry-command")
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
                StepSpec(0, "prepare()", "plan-1", "plan-1-step-0", tool_name="prepare"),
                StepSpec(
                    1,
                    "long_running_tool()",
                    "plan-1",
                    "plan-1-step-1",
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
                retryable=True,
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
    assert retried.runtime_session_cleanup_status == RuntimeSessionCleanupStatus.NOT_REQUIRED
    assert [step.status for step in retried.steps] == [
        StepStatus.PENDING,
        StepStatus.PENDING,
    ]


async def test_infrastructure_retry_waits_for_abandoned_kernel_cleanup(
    execution_service: ExecutionService,
    engine: AsyncEngine,
) -> None:
    execution = await execution_service.submit(submit_command("cleanup-pending-retry-submit"))
    now = utc_now()
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution.id)
            .values(
                status=ExecutionStatus.FAILED,
                failure_type=FailureType.LEASE_EXPIRED,
                retryable=True,
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
    assert unchanged.runtime_session_cleanup_status == RuntimeSessionCleanupStatus.PENDING
