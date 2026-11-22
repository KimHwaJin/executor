from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import event, update
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
    ExecutionStatus,
    FailureType,
    OperationMode,
    OperationStatus,
    RetryStrategy,
    RuntimeSessionCleanupStatus,
    RuntimeType,
    StepStatus,
    TriggerType,
)
from executor_service.domain.errors import (
    ExecutionNotFoundError,
    ExecutionVersionConflictError,
    IdempotencyConflictError,
    InvalidExecutionSpecError,
    InvalidStateTransitionError,
    UnsupportedRuntimeProfileError,
)
from executor_service.domain.models import Execution, utc_now
from executor_service.domain.runtime import RuntimeOffering
from executor_service.infrastructure.db.models import (
    ExecutionOperationORM,
    ExecutionORM,
    ExecutionStepORM,
)
from executor_service.infrastructure.db.repositories import (
    SQLAlchemyUnitOfWork,
)
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.result_storage import (
    FilesystemExecutionResultStore,
)
from executor_service.infrastructure.runtime_adapters import (
    JupyterRuntimeAdapter,
)


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
        tenant_id="test-tenant",
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


async def test_step_limits_apply_to_submit_and_appended_operations(
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    session_factory = create_session_factory(engine)
    service = ExecutionService(
        lambda: SQLAlchemyUnitOfWork(session_factory),
        {
            RuntimeType.JUPYTER: RuntimeOffering(
                ("basic",), JupyterRuntimeAdapter.capabilities
            )
        },
        FilesystemExecutionResultStore(tmp_path),
        max_steps_per_operation=2,
        max_steps_per_execution=2,
    )

    too_many_initial_steps = replace(
        submit_command("too-many-initial-steps"),
        steps=tuple(
            StepSpec(sequence=index, code=f"value_{index} = {index}")
            for index in range(3)
        ),
    )
    with pytest.raises(
        InvalidExecutionSpecError, match="at most 2 Steps; this one has 3"
    ) as per_operation:
        await service.submit(too_many_initial_steps)
    [issue] = per_operation.value.details
    assert issue.location == ("body", "operation", "spec", "steps")
    assert issue.type == "too_many_steps"
    assert issue.context == {"limit": 2, "actual": 3}

    execution = await service.submit(multi_submit_command("bounded-multi"))
    async with session_factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution.id)
            .values(status=ExecutionStatus.WAITING_FOR_OPERATION, version=2)
        )

    # The same 422 as the per-Operation limit, with the counts behind it.
    with pytest.raises(
        InvalidExecutionSpecError, match="at most 2 Steps; it has 1"
    ) as per_execution:
        await service.create_operation(
            CreateOperationCommand(
                execution_id=execution.id,
                idempotency_key="too-many-total-steps",
                expected_version=2,
                steps=(
                    StepSpec(sequence=1, code="value_1 = 1"),
                    StepSpec(sequence=2, code="value_2 = 2"),
                ),
            )
        )
    [issue] = per_execution.value.details
    assert issue.location == ("body", "spec", "steps")
    assert issue.type == "execution_step_limit_exceeded"
    assert issue.context == {
        "limit": 2,
        "existing": 1,
        "requested": 2,
        "actual": 3,
    }


async def test_timeouts_over_the_execution_maximum_runtime_are_rejected(
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    session_factory = create_session_factory(engine)
    service = ExecutionService(
        lambda: SQLAlchemyUnitOfWork(session_factory),
        {
            RuntimeType.JUPYTER: RuntimeOffering(
                ("basic",), JupyterRuntimeAdapter.capabilities
            )
        },
        FilesystemExecutionResultStore(tmp_path),
        max_runtime_seconds=3600,
    )
    with pytest.raises(
        InvalidExecutionSpecError, match="operation_timeout_seconds"
    ):
        await service.submit(
            replace(
                submit_command("operation-over-runtime"),
                operation_timeout_seconds=3601,
            )
        )
    base = submit_command("step-over-runtime")
    with pytest.raises(
        InvalidExecutionSpecError, match=r"steps\[0\].step_timeout_seconds"
    ):
        await service.submit(
            replace(
                base,
                steps=(replace(base.steps[0], step_timeout_seconds=3601),),
            )
        )
    # Equal to the maximum runtime is accepted.
    accepted = await service.submit(
        replace(
            submit_command("operation-at-runtime"),
            operation_timeout_seconds=3600,
        )
    )
    assert accepted.status == ExecutionStatus.QUEUED

    execution = await service.submit(multi_submit_command("multi-runtime"))
    async with session_factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution.id)
            .values(status=ExecutionStatus.WAITING_FOR_OPERATION, version=2)
        )
    with pytest.raises(InvalidExecutionSpecError):
        await service.create_operation(
            CreateOperationCommand(
                execution_id=execution.id,
                idempotency_key="append-over-runtime",
                expected_version=2,
                operation_timeout_seconds=7200,
                steps=(StepSpec(sequence=1, code="value_1 = 1"),),
            )
        )


async def test_commands_that_change_no_step_update_no_step_row(
    execution_service: ExecutionService,
    engine: AsyncEngine,
) -> None:
    """An Operation or a cancel must not cost one UPDATE per Step."""
    session_factory = create_session_factory(engine)
    execution = await execution_service.submit(
        multi_submit_command("no-step-updates")
    )
    step_updates: list[str] = []

    def record(_conn, _cursor, statement, *_args) -> None:
        if statement.startswith("UPDATE execution_steps"):
            step_updates.append(statement)

    async with session_factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution.id)
            .values(status=ExecutionStatus.WAITING_FOR_OPERATION, version=2)
        )

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        continued = await execution_service.create_operation(
            CreateOperationCommand(
                execution_id=execution.id,
                idempotency_key="second-operation",
                expected_version=2,
                steps=(StepSpec(sequence=1, code="value_1 = 1"),),
            )
        )
        await execution_service.cancel(
            CancelExecutionCommand(
                execution_id=execution.id,
                idempotency_key="cancel",
                reason=None,
            )
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)

    assert [step.sequence for step in continued.steps] == [0, 1]
    assert step_updates == []


async def _waiting_multi(
    service: ExecutionService, engine: AsyncEngine, key: str
) -> Execution:
    execution = await service.submit(multi_submit_command(key))
    async with create_session_factory(engine)() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution.id)
            .values(status=ExecutionStatus.WAITING_FOR_OPERATION, version=2)
        )
    return execution


def _during_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    action: Callable[[], Awaitable[None]],
) -> None:
    """Run the action once, while a source snapshot is being written."""
    snapshot = FilesystemExecutionResultStore.snapshot_sources
    pending = [action]

    async def snapshot_sources(self, *args):
        if pending:
            await pending.pop()()
        return await snapshot(self, *args)

    monkeypatch.setattr(
        FilesystemExecutionResultStore, "snapshot_sources", snapshot_sources
    )


async def test_source_snapshots_hold_no_database_connection(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = await _waiting_multi(execution_service, engine, "held")
    held = 0
    held_during_snapshot: list[int] = []

    def checkout(*_args) -> None:
        nonlocal held
        held += 1

    def checkin(*_args) -> None:
        nonlocal held
        held -= 1

    snapshot = FilesystemExecutionResultStore.snapshot_sources

    async def snapshot_sources(self, *args):
        held_during_snapshot.append(held)
        return await snapshot(self, *args)

    monkeypatch.setattr(
        FilesystemExecutionResultStore, "snapshot_sources", snapshot_sources
    )
    event.listen(engine.sync_engine, "checkout", checkout)
    event.listen(engine.sync_engine, "checkin", checkin)
    try:
        await execution_service.submit(submit_command("no-connection"))
        await execution_service.create_operation(
            CreateOperationCommand(
                execution_id=execution.id,
                idempotency_key="no-connection-operation",
                expected_version=2,
                steps=(StepSpec(sequence=1, code="value_1 = 1"),),
            )
        )
    finally:
        event.remove(engine.sync_engine, "checkout", checkout)
        event.remove(engine.sync_engine, "checkin", checkin)

    assert held_during_snapshot == [0, 0]


async def test_submit_repeated_while_its_sources_are_written_returns_one(
    execution_service: ExecutionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = submit_command("concurrent-submit")
    concurrent: list[Execution] = []

    async def same_request() -> None:
        concurrent.append(await execution_service.submit(command))

    _during_snapshot(monkeypatch, same_request)
    submitted = await execution_service.submit(command)

    assert submitted.id == concurrent[0].id


async def test_operation_checks_the_state_again_after_its_sources_are_written(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = await _waiting_multi(execution_service, engine, "raced")

    async def cancel() -> None:
        await execution_service.cancel(
            CancelExecutionCommand(
                execution_id=execution.id,
                idempotency_key="cancel-during-snapshot",
                reason=None,
            )
        )

    _during_snapshot(monkeypatch, cancel)
    with pytest.raises(
        InvalidStateTransitionError, match="must be WAITING_FOR_OPERATION"
    ):
        await execution_service.create_operation(
            CreateOperationCommand(
                execution_id=execution.id,
                idempotency_key="raced-operation",
                expected_version=2,
                steps=(StepSpec(sequence=1, code="value_1 = 1"),),
            )
        )
    stored = await execution_service.get(execution.id)
    assert stored.status == ExecutionStatus.CANCEL_REQUESTED
    assert len(stored.steps) == 1


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


async def test_command_keys_are_scoped_to_the_execution(
    execution_service: ExecutionService,
) -> None:
    first = await execution_service.submit(submit_command("scope-first"))
    second = await execution_service.submit(submit_command("scope-second"))
    for execution in (first, second):
        cancelled = await execution_service.cancel(
            CancelExecutionCommand(
                execution_id=execution.id,
                idempotency_key="shared-cancel-key",
                reason="user requested",
            )
        )
        assert cancelled.id == execution.id
        assert cancelled.status == ExecutionStatus.CANCEL_REQUESTED


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


async def test_submit_records_the_calling_user_and_client(
    execution_service: ExecutionService,
) -> None:
    command = replace(
        submit_command("audit-from-caller"),
        actor_client="pr-batch",
        actor_id="user-1",
    )

    submitted = await execution_service.submit(command)

    assert submitted.created_by_client == "pr-batch"
    assert submitted.created_by == "user-1"
    assert submitted.updated_by_client == "pr-batch"
    assert submitted.updated_by == "user-1"


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
        await session.execute(
            update(ExecutionStepORM)
            .where(ExecutionStepORM.execution_id == execution.id)
            .values(status=StepStatus.SUCCEEDED)
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
                runtime_session_id=None,
                runtime_target_id=uuid4(),
                runtime_session_cleanup_status=RuntimeSessionCleanupStatus.SUCCEEDED,
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


@pytest.mark.parametrize(
    "cleanup_status",
    [
        RuntimeSessionCleanupStatus.PENDING,
        RuntimeSessionCleanupStatus.FAILED,
    ],
)
async def test_infrastructure_retry_waits_for_abandoned_runtime_session_cleanup(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    cleanup_status: RuntimeSessionCleanupStatus,
) -> None:
    execution = await execution_service.submit(
        submit_command(f"cleanup-{cleanup_status.value}-retry-submit")
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
                runtime_session_cleanup_status=cleanup_status,
                finished_at=now,
            )
        )

    with pytest.raises(InvalidStateTransitionError, match="unresolved"):
        await execution_service.retry(
            RetryExecutionCommand(
                execution_id=execution.id,
                idempotency_key=(
                    f"cleanup-{cleanup_status.value}-retry-command"
                ),
            )
        )

    unchanged = await execution_service.get(execution.id)
    assert unchanged.status == ExecutionStatus.FAILED
    assert unchanged.runtime_session_cleanup_status == cleanup_status


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


async def test_accepted_command_reports_its_commit_and_a_rejected_one_does_not(
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    session_factory = create_session_factory(engine)
    commits = 0

    def committed() -> None:
        nonlocal commits
        commits += 1

    service = ExecutionService(
        lambda: SQLAlchemyUnitOfWork(session_factory, committed=committed),
        {
            RuntimeType.JUPYTER: RuntimeOffering(
                ("basic",), JupyterRuntimeAdapter.capabilities
            )
        },
        FilesystemExecutionResultStore(tmp_path),
    )

    with pytest.raises(UnsupportedRuntimeProfileError):
        await service.submit(
            replace(submit_command("wake-rejected"), runtime_profile="unknown")
        )
    assert commits == 0

    await service.submit(submit_command("wake-accepted"))
    assert commits == 1
