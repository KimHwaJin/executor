import asyncio
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.commands import (
    CreateOperationCommand,
    StepSpec,
    SubmitExecutionCommand,
)
from executor_service.application.services import ExecutionService
from executor_service.config import Settings
from executor_service.domain.enums import (
    AttemptStatus,
    ExecutionStatus,
    FailureType,
    OperationMode,
    OperationStatus,
    RuntimePool,
    RuntimeSessionCleanupStatus,
    RuntimeTargetStatus,
    StepStatus,
    TriggerType,
)
from executor_service.domain.errors import InvalidStateTransitionError
from executor_service.domain.models import Execution, utc_now
from executor_service.domain.runtime import (
    RuntimeExecutionError,
    RuntimeExecutionResult,
    RuntimeExecutionTimeoutError,
    RuntimeResourceMetric,
    RuntimeResourceObservation,
)
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionOperationORM,
    ExecutionORM,
    ExecutionStepAttemptORM,
    ExecutionStepORM,
    OutboxEventORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.runtime_registry import (
    RuntimeTargetRegistry,
)
from executor_service.infrastructure.worker import ExecutionWorker
from tests.runtime_credentials import runtime_credential_fields
from tests.runtime_storage_fake import InMemoryRuntimeStorage


class FakeJupyterGateway:
    session_exists_result = True
    deleted: ClassVar[list[str]] = []
    interrupted: ClassVar[list[str]] = []

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    async def close(self) -> None:
        pass

    async def session_exists(self, _runtime_session_id: str) -> bool:
        return self.session_exists_result

    async def interrupt_session(self, runtime_session_id: str) -> None:
        self.interrupted.append(runtime_session_id)

    async def delete_session(self, runtime_session_id: str) -> None:
        self.deleted.append(runtime_session_id)


class RecordingMultiDriver(InMemoryRuntimeStorage):
    executed: ClassVar[list[str]] = []
    fail_code: ClassVar[str | None] = None

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    async def close(self) -> None:
        pass

    async def status(self) -> dict[str, Any]:
        return {"status": "ok"}

    async def supported_profiles(self) -> list[str]:
        return ["basic"]

    async def resource_status(self) -> RuntimeResourceObservation:
        empty = RuntimeResourceMetric(
            used=None,
            capacity=None,
            utilization=None,
            source=None,
            estimated=None,
        )
        return RuntimeResourceObservation(
            observed_at=utc_now(),
            process_count=None,
            cpu=empty,
            memory=empty,
        )

    async def start_session(self, profile: str, working_directory: str) -> str:
        del profile, working_directory
        return "operation-kernel"

    async def delete_session(self, session_id: str) -> None:
        del session_id

    async def interrupt_session(self, session_id: str) -> None:
        del session_id

    async def session_exists(self, session_id: str) -> bool:
        del session_id
        return True

    async def execute(
        self, session_id: str, code: str
    ) -> RuntimeExecutionResult:
        del session_id
        self.executed.append(code)
        if code == self.fail_code:
            raise RuntimeExecutionError(
                "expected operation failure",
                [
                    {
                        "output_type": "error",
                        "ename": "RuntimeError",
                        "evalue": "expected",
                        "traceback": ["RuntimeError: expected"],
                    }
                ],
            )
        return RuntimeExecutionResult(
            outputs=[
                {
                    "output_type": "stream",
                    "name": "stdout",
                    "text": f"{code}\n",
                }
            ],
            execution_count=len(self.executed),
        )


class SlowExecutionDriver(RecordingMultiDriver):
    async def execute(
        self, session_id: str, code: str
    ) -> RuntimeExecutionResult:
        del session_id, code
        await asyncio.sleep(2)
        return RuntimeExecutionResult(outputs=[], execution_count=1)


def _patch_runtime_driver(
    monkeypatch: pytest.MonkeyPatch, driver_type: type[Any]
) -> None:
    monkeypatch.setattr(
        "executor_service.infrastructure.runtime_drivers.JupyterRuntimeDriver",
        driver_type,
    )


def _multi_command(key: str) -> SubmitExecutionCommand:
    code = "value = 1"
    return SubmitExecutionCommand(
        idempotency_key=key,
        operation_mode=OperationMode.MULTI,
        operation_wait_timeout_seconds=3600,
        trigger_type=TriggerType.INTERACTIVE,
        runtime_profile="basic",
        user_id="lifecycle-user",
        project_id="lifecycle-project",
        session_id="lifecycle-session",
        task_id="test-task",
        steps=(
            StepSpec(
                sequence=0,
                code=code,
                tool_name="initialize",
            ),
        ),
    )


async def _make_waiting(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    key: str,
    *,
    wait_expired: bool = False,
    execution_expired: bool = False,
    server_enabled: bool = True,
) -> tuple[Execution, ExecutionAttemptORM]:
    execution = await execution_service.submit(_multi_command(key))
    now = utc_now()
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        target = RuntimeTargetORM(
            name=f"target-{key}",
            connection_config={"endpoint": "http://fake-jupyter"},
            **runtime_credential_fields(),
            pool=RuntimePool.INTERACTIVE,
            status=(
                RuntimeTargetStatus.ACTIVE
                if server_enabled
                else RuntimeTargetStatus.OFFLINE
            ),
            max_concurrent_executions=2,
            supported_profiles=["basic"],
            enabled=server_enabled,
        )
        session.add(target)
        await session.flush()
        attempt = ExecutionAttemptORM(
            execution_id=execution.id,
            attempt_number=1,
            runtime_target_id=target.id,
            runtime_session_id=f"kernel-{key}",
            status=AttemptStatus.WAITING,
            lease_owner=None,
            lease_expires_at=None,
            heartbeat_at=now,
            started_at=now - timedelta(minutes=1),
        )
        session.add(attempt)
        await session.flush()
        session.add(
            ExecutionStepAttemptORM(
                execution_id=execution.id,
                execution_attempt_id=attempt.id,
                execution_step_id=execution.steps[0].id,
                sequence=0,
                tool_name="initialize",
                input_parameters={},
                status=StepStatus.SUCCEEDED,
                outputs=[],
                started_at=now - timedelta(minutes=1),
                finished_at=now,
            )
        )
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution.id)
            .values(
                status=ExecutionStatus.WAITING_FOR_OPERATION,
                runtime_target_id=target.id,
                runtime_session_id=attempt.runtime_session_id,
                started_at=now - timedelta(minutes=1),
                operation_wait_expires_at=(
                    now - timedelta(seconds=1)
                    if wait_expired
                    else now + timedelta(hours=1)
                ),
                execution_expires_at=(
                    now - timedelta(seconds=1)
                    if execution_expired
                    else now + timedelta(days=1)
                ),
                version=2,
            )
        )
        await session.execute(
            update(ExecutionStepORM)
            .where(ExecutionStepORM.id == execution.steps[0].id)
            .values(status=StepStatus.SUCCEEDED, finished_at=now)
        )
    return execution, attempt


def _worker(
    engine: AsyncEngine, tmp_path: Path
) -> tuple[ExecutionWorker, Redis]:
    settings = Settings(
        runtime_enabled=False,
        input_host_root=tmp_path,
        execution_lease_seconds=30,
        execution_heartbeat_seconds=5,
        jupyter_request_timeout_seconds=0.1,
    )
    session_factory = create_session_factory(engine)
    redis = Redis.from_url("redis://127.0.0.1:6379/15", decode_responses=True)
    registry = RuntimeTargetRegistry(session_factory, settings)
    worker = ExecutionWorker(
        session_factory=session_factory,
        redis=redis,
        settings=settings,
        registry=registry,
        artifact_manager=ExecutionArtifactManager(session_factory),
    )
    return worker, redis


async def test_runtime_step_enforces_operation_and_step_timeouts(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    command = replace(
        _multi_command("runtime-timeout-contract"),
        operation_timeout_seconds=1,
        steps=(StepSpec(0, "slow()", step_timeout_seconds=1),),
    )
    execution = await execution_service.submit(command)
    operation_id = execution.active_operation_id
    assert operation_id is not None
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        await session.execute(
            update(ExecutionOperationORM)
            .where(ExecutionOperationORM.id == operation_id)
            .values(started_at=utc_now() - timedelta(seconds=2))
        )

    worker, redis = _worker(engine, tmp_path)
    try:
        with pytest.raises(RuntimeExecutionTimeoutError) as operation_error:
            await worker._execute_runtime_step(
                RecordingMultiDriver(),
                "runtime-session",
                "slow()",
                execution.id,
                0,
            )
        assert operation_error.value.scope == "Operation"

        async with session_factory() as session, session.begin():
            await session.execute(
                update(ExecutionOperationORM)
                .where(ExecutionOperationORM.id == operation_id)
                .values(operation_timeout_seconds=None, started_at=utc_now())
            )
        with pytest.raises(RuntimeExecutionTimeoutError) as step_error:
            await worker._execute_runtime_step(
                SlowExecutionDriver(),
                "runtime-session",
                "slow()",
                execution.id,
                0,
            )
        assert step_error.value.scope == "Step"
    finally:
        await redis.aclose()


@pytest.fixture(autouse=True)
def _reset_fake_gateway() -> None:
    RecordingMultiDriver.reset_storage()
    FakeJupyterGateway.session_exists_result = True
    FakeJupyterGateway.deleted = []
    FakeJupyterGateway.interrupted = []
    RecordingMultiDriver.executed = []
    RecordingMultiDriver.fail_code = None


@pytest.mark.parametrize(
    ("fail_code", "expected_status", "expected_steps", "expected_event"),
    [
        (
            None,
            OperationStatus.SUCCEEDED,
            [StepStatus.SUCCEEDED, StepStatus.SUCCEEDED, StepStatus.SUCCEEDED],
            "execution.operation_succeeded",
        ),
        (
            "raise expected",
            OperationStatus.FAILED,
            [StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.SKIPPED],
            "execution.operation_failed",
        ),
    ],
)
async def test_multi_operation_executes_submitted_steps_until_boundary(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_code: str | None,
    expected_status: OperationStatus,
    expected_steps: list[StepStatus],
    expected_event: str,
) -> None:
    command = _multi_command(f"operation-{expected_status.value.lower()}")
    command = replace(
        command,
        steps=(
            StepSpec(0, "first"),
            StepSpec(1, "raise expected"),
            StepSpec(2, "third"),
        ),
    )
    execution = await execution_service.submit(command)
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        session.add(
            RuntimeTargetORM(
                name=f"operation-target-{expected_status.value.lower()}",
                connection_config={"endpoint": "http://operation.invalid"},
                **runtime_credential_fields(),
                pool=RuntimePool.INTERACTIVE,
                status=RuntimeTargetStatus.ACTIVE,
                max_concurrent_executions=1,
                supported_profiles=["basic"],
                enabled=True,
            )
        )
    RecordingMultiDriver.fail_code = fail_code
    _patch_runtime_driver(monkeypatch, RecordingMultiDriver)
    worker, redis = _worker(engine, tmp_path)
    try:
        await worker._run_execution(execution.id)
    finally:
        await redis.aclose()

    async with session_factory() as session:
        row = await session.get(ExecutionORM, execution.id)
        operation = await session.get(
            ExecutionOperationORM, execution.active_operation_id
        )
        steps = list(
            await session.scalars(
                select(ExecutionStepORM)
                .where(ExecutionStepORM.execution_id == execution.id)
                .order_by(ExecutionStepORM.sequence)
            )
        )
        events = list(
            await session.scalars(
                select(OutboxEventORM).where(
                    OutboxEventORM.aggregate_id == execution.id,
                    OutboxEventORM.event_type == expected_event,
                )
            )
        )
        step_result_events = list(
            await session.scalars(
                select(OutboxEventORM)
                .where(
                    OutboxEventORM.aggregate_id == execution.id,
                    OutboxEventORM.event_type.in_(
                        ["execution.step_succeeded", "execution.step_failed"]
                    ),
                )
                .order_by(OutboxEventORM.created_at)
            )
        )
        ordered_event_types = list(
            await session.scalars(
                select(OutboxEventORM.event_type)
                .where(OutboxEventORM.aggregate_id == execution.id)
                .order_by(OutboxEventORM.created_at, OutboxEventORM.id)
            )
        )
    assert row is not None and operation is not None
    assert row.status == ExecutionStatus.WAITING_FOR_OPERATION, (
        row.error_message
    )
    assert operation.status == expected_status
    assert [step.status for step in steps] == expected_steps
    assert RecordingMultiDriver.executed == (
        ["first", "raise expected", "third"]
        if fail_code is None
        else ["first", "raise expected"]
    )
    notebook_path = next(iter(RecordingMultiDriver.notebooks))
    notebook = RecordingMultiDriver.notebooks[notebook_path]
    assert [cell["source"] for cell in notebook["cells"]] == (
        ["first", "raise expected", "third"]
        if fail_code is None
        else ["first", "raise expected"]
    )
    assert [cell["execution_count"] for cell in notebook["cells"]] == (
        [1, 2, 3] if fail_code is None else [1, 2]
    )
    assert all(cell["outputs"] for cell in notebook["cells"])
    assert len(events) == 1
    assert events[0].payload["operation_id"] == str(operation.id)
    assert len(step_result_events) == (3 if fail_code is None else 2)
    assert [event.payload["sequence"] for event in step_result_events] == list(
        range(len(step_result_events))
    )
    for index, event in enumerate(step_result_events, start=1):
        assert event.payload["operation_id"] == str(operation.id)
        assert event.payload["step_id"] == str(steps[index - 1].id)
        assert event.payload["result_available"] is True
        assert event.payload["result_ref"]["scope"] == "STEP"
        assert event.payload["output_summary"]["output_count"] > 0
        assert event.payload.get("execution_count") == (
            None if event.event_type == "execution.step_failed" else index
        )
    result_positions = [
        index
        for index, event_type in enumerate(ordered_event_types)
        if event_type in {"execution.step_succeeded", "execution.step_failed"}
    ]
    assert max(result_positions) < ordered_event_types.index(expected_event)


async def test_expired_multi_wait_fails_and_cleans_kernel_once(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution, _ = await _make_waiting(
        execution_service, engine, "wait-timeout", wait_expired=True
    )
    _patch_runtime_driver(monkeypatch, FakeJupyterGateway)
    worker, redis = _worker(engine, tmp_path)
    try:
        await worker._audit_multi_lifecycle()
        await worker._audit_multi_lifecycle()
    finally:
        await redis.aclose()

    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        row = await session.get(ExecutionORM, execution.id)
        attempt = await session.scalar(
            select(ExecutionAttemptORM).where(
                ExecutionAttemptORM.execution_id == execution.id
            )
        )
        failed_events = await session.scalar(
            select(func.count(OutboxEventORM.id)).where(
                OutboxEventORM.aggregate_id == execution.id,
                OutboxEventORM.event_type == "execution.failed",
            )
        )
    assert row is not None and attempt is not None
    assert row.status == ExecutionStatus.FAILED
    assert row.failure_type == FailureType.OPERATION_WAIT_TIMEOUT
    assert row.runtime_session_id is None
    assert (
        row.runtime_session_cleanup_status
        == RuntimeSessionCleanupStatus.SUCCEEDED
    )
    assert attempt.status == AttemptStatus.FAILED
    assert failed_events == 1
    assert FakeJupyterGateway.deleted == ["kernel-wait-timeout"]
    with pytest.raises(InvalidStateTransitionError):
        await execution_service.create_operation(
            CreateOperationCommand(
                execution_id=execution.id,
                idempotency_key="continue-after-timeout",
                expected_version=row.version,
                steps=(
                    StepSpec(
                        sequence=1,
                        code="print('too late')",
                    ),
                ),
            )
        )


async def test_restart_audit_detects_missing_kernel_without_cleanup(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution, _ = await _make_waiting(
        execution_service, engine, "kernel-lost"
    )
    FakeJupyterGateway.session_exists_result = False
    _patch_runtime_driver(monkeypatch, FakeJupyterGateway)
    worker, redis = _worker(engine, tmp_path)
    try:
        await worker._audit_multi_lifecycle()
    finally:
        await redis.aclose()

    async with create_session_factory(engine)() as session:
        row = await session.get(ExecutionORM, execution.id)
    assert row is not None
    assert row.status == ExecutionStatus.FAILED
    assert row.failure_type == FailureType.RUNTIME_SESSION_LOST
    assert row.runtime_session_id is None
    assert (
        row.runtime_session_cleanup_status
        == RuntimeSessionCleanupStatus.NOT_REQUIRED
    )
    assert FakeJupyterGateway.deleted == []


async def test_disabled_target_fails_waiting_execution(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution, _ = await _make_waiting(
        execution_service, engine, "target-disabled", server_enabled=False
    )
    _patch_runtime_driver(monkeypatch, FakeJupyterGateway)
    worker, redis = _worker(engine, tmp_path)
    try:
        await worker._audit_multi_lifecycle()
    finally:
        await redis.aclose()

    async with create_session_factory(engine)() as session:
        row = await session.get(ExecutionORM, execution.id)
    assert row is not None
    assert row.status == ExecutionStatus.FAILED
    assert row.failure_type == FailureType.RUNTIME_UNAVAILABLE
    assert (
        row.runtime_session_cleanup_status
        == RuntimeSessionCleanupStatus.SUCCEEDED
    )


async def test_execution_deadline_precedes_step_wait_deadline(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution, _ = await _make_waiting(
        execution_service,
        engine,
        "execution-timeout",
        wait_expired=True,
        execution_expired=True,
    )
    _patch_runtime_driver(monkeypatch, FakeJupyterGateway)
    worker, redis = _worker(engine, tmp_path)
    try:
        await worker._audit_multi_lifecycle()
    finally:
        await redis.aclose()

    async with create_session_factory(engine)() as session:
        row = await session.get(ExecutionORM, execution.id)
    assert row is not None
    assert row.status == ExecutionStatus.FAILED
    assert row.failure_type == FailureType.EXECUTION_TIMEOUT


async def test_running_execution_deadline_requests_cancel_and_reclaims_kernel(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution, _ = await _make_waiting(
        execution_service, engine, "running-timeout"
    )
    now = utc_now()
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution.id)
            .values(
                status=ExecutionStatus.RUNNING,
                execution_expires_at=now - timedelta(seconds=1),
                lease_owner="slow-worker",
                lease_expires_at=now + timedelta(minutes=1),
            )
        )
        await session.execute(
            update(ExecutionAttemptORM)
            .where(ExecutionAttemptORM.execution_id == execution.id)
            .values(
                status=AttemptStatus.RUNNING,
                lease_owner="slow-worker",
                lease_expires_at=now + timedelta(minutes=1),
            )
        )
    _patch_runtime_driver(monkeypatch, FakeJupyterGateway)
    worker, redis = _worker(engine, tmp_path)
    try:
        await worker._audit_multi_lifecycle()
        for _ in range(50):
            current = await execution_service.get(execution.id)
            if current.status == ExecutionStatus.CANCELLED:
                break
            await asyncio.sleep(0.01)
        if worker._jobs:
            await asyncio.gather(*list(worker._jobs.values()))
    finally:
        await redis.aclose()

    async with session_factory() as session:
        row = await session.get(ExecutionORM, execution.id)
        timeout_events = await session.scalar(
            select(func.count(OutboxEventORM.id)).where(
                OutboxEventORM.aggregate_id == execution.id,
                OutboxEventORM.event_type == "execution.timeout_requested",
            )
        )
    assert row is not None
    assert row.status == ExecutionStatus.CANCELLED
    assert row.cancellation_reason == "Execution exceeded its maximum runtime."
    assert row.runtime_session_id is None
    assert timeout_events == 1
    assert FakeJupyterGateway.interrupted == ["kernel-running-timeout"]
    assert FakeJupyterGateway.deleted == ["kernel-running-timeout"]
