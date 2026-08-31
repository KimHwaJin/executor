"""Worker consistency when Runtime output delivery hits size/rate limits."""

import json
from pathlib import Path
from typing import Any, cast

import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.commands import (
    StepSpec,
    SubmitExecutionCommand,
)
from executor_service.application.services import ExecutionService
from executor_service.config import Settings
from executor_service.domain.enums import (
    ExecutionStatus,
    FailureType,
    OperationMode,
    OperationStatus,
    RetryStrategy,
    RuntimeAbortStatus,
    RuntimePool,
    RuntimeSessionCleanupStatus,
    RuntimeTargetStatus,
    RuntimeType,
    StepStatus,
    TriggerType,
)
from executor_service.domain.runtime import (
    RuntimeAbortResult,
    RuntimeDriverFactory,
    RuntimeOutputLimitExceededError,
    RuntimeOutputLimitKind,
)
from executor_service.infrastructure.artifacts import (
    ExecutionArtifactManager,
)
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionOperationORM,
    ExecutionStepORM,
    OutboxEventORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.execution_worker import ExecutionWorker
from executor_service.infrastructure.runtime_registry import (
    RuntimeTargetRegistry,
)
from tests.runtime_credentials import runtime_credential_fields
from tests.runtime_storage_fake import InMemoryRuntimeStorage
from tests.test_jupyter_output_limits import rate_warning


class OutputLimitRuntimeDriver(InMemoryRuntimeStorage):
    def __init__(
        self,
        max_message_bytes: int,
        kind: RuntimeOutputLimitKind,
        abort_status: RuntimeAbortStatus,
    ) -> None:
        self.max_message_bytes = max_message_bytes
        self.kind = kind
        self.abort_status = abort_status

    async def start_session(
        self, _runtime_profile: str, _working_directory: str
    ) -> str:
        return "output-limit-kernel"

    async def execute(self, _session_id: str, _code: str) -> Any:
        if self.kind == "MESSAGE_SIZE":
            raise RuntimeOutputLimitExceededError(self.max_message_bytes)
        raise RuntimeOutputLimitExceededError(
            kind=self.kind,
            outputs=[
                {
                    "output_type": "stream",
                    "name": "stderr",
                    "text": rate_warning(self.kind),
                }
            ],
        )

    async def abort_session(
        self, _session_id: str, _timeout_seconds: float
    ) -> RuntimeAbortResult:
        return RuntimeAbortResult(self.abort_status)

    async def delete_session(self, _session_id: str) -> None:
        pass

    async def close(self) -> None:
        pass


class OutputLimitDriverFactory:
    def __init__(self, driver: OutputLimitRuntimeDriver) -> None:
        self.driver = driver

    def create(
        self,
        _runtime_type: RuntimeType,
        _connection_config: dict[str, Any],
        _credential: str,
    ) -> Any:
        return self.driver


@pytest.mark.parametrize("mode", [OperationMode.SINGLE, OperationMode.MULTI])
@pytest.mark.parametrize("kind", ["MESSAGE_SIZE", "DATA_RATE", "MESSAGE_RATE"])
@pytest.mark.parametrize(
    "abort_status",
    [RuntimeAbortStatus.IDLE_CONFIRMED, RuntimeAbortStatus.SESSION_MISSING],
)
async def test_output_limit_aborts_runtime_and_persists_incomplete_result(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    mode: OperationMode,
    kind: RuntimeOutputLimitKind,
    abort_status: RuntimeAbortStatus,
) -> None:
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        session.add(
            RuntimeTargetORM(
                name="output-limit-runtime",
                connection_config={"endpoint": "http://runtime.invalid"},
                **runtime_credential_fields(),
                pool=RuntimePool.INTERACTIVE,
                status=RuntimeTargetStatus.ACTIVE,
                max_concurrent_executions=1,
                supported_profiles=["basic"],
                enabled=True,
            )
        )
    execution = await execution_service.submit(
        SubmitExecutionCommand(
            idempotency_key="output-limit-single",
            operation_mode=mode,
            operation_wait_timeout_seconds=600
            if mode == OperationMode.MULTI
            else None,
            trigger_type=TriggerType.INTERACTIVE,
            runtime_profile="basic",
            user_id="output-user",
            project_id=None,
            session_id=None,
            task_id="output-task",
            steps=(
                StepSpec(sequence=0, code="print('large')"),
                StepSpec(sequence=1, code="print('must not run')"),
            ),
        )
    )
    settings = Settings(
        runtime_enabled=False,
        shared_storage_root=tmp_path,
        runtime_max_output_message_bytes=1048576,
    )
    redis = Redis.from_url("redis://127.0.0.1:6379/15", decode_responses=True)
    driver = OutputLimitRuntimeDriver(
        settings.runtime_max_output_message_bytes,
        kind,
        abort_status,
    )
    worker = ExecutionWorker(
        session_factory=session_factory,
        redis=redis,
        settings=settings,
        registry=RuntimeTargetRegistry(session_factory, settings),
        artifact_manager=ExecutionArtifactManager(session_factory),
        driver_factory=cast(
            RuntimeDriverFactory,
            OutputLimitDriverFactory(driver),
        ),
    )
    try:
        await worker._runner.run(execution.id)
    finally:
        await redis.aclose()

    finished = await execution_service.get(execution.id)
    retained = abort_status == RuntimeAbortStatus.IDLE_CONFIRMED
    waiting = mode == OperationMode.MULTI and retained
    assert finished.status == (
        ExecutionStatus.WAITING_FOR_OPERATION
        if waiting
        else ExecutionStatus.FAILED
    )
    assert finished.failure_type == FailureType.OUTPUT_LIMIT_EXCEEDED
    if mode == OperationMode.SINGLE:
        assert finished.retry_strategy == (
            RetryStrategy.FROM_FAILED_STEP
            if retained
            else RetryStrategy.FROM_START
        )
    assert finished.runtime_abort_status == abort_status
    assert finished.runtime_session_cleanup_status == (
        RuntimeSessionCleanupStatus.NOT_REQUIRED
        if retained
        else RuntimeSessionCleanupStatus.SUCCEEDED
    )

    async with session_factory() as session:
        attempt = await session.scalar(
            select(ExecutionAttemptORM).where(
                ExecutionAttemptORM.execution_id == execution.id
            )
        )
        step = await session.scalar(
            select(ExecutionStepORM).where(
                ExecutionStepORM.execution_id == execution.id,
                ExecutionStepORM.sequence == 0,
            )
        )
        event_types = set(
            await session.scalars(
                select(OutboxEventORM.event_type).where(
                    OutboxEventORM.aggregate_id == execution.id
                )
            )
        )
        operation = await session.get(
            ExecutionOperationORM, execution.active_operation_id
        )

    assert attempt is not None
    assert attempt.failure_type == FailureType.OUTPUT_LIMIT_EXCEEDED
    assert operation is not None and operation.status == OperationStatus.FAILED
    assert finished.steps[1].status == StepStatus.SKIPPED
    assert step is not None
    assert step.status == StepStatus.FAILED
    assert step.result_complete is False
    assert step.result_manifest_path is not None
    manifest = json.loads((tmp_path / step.result_manifest_path).read_bytes())
    assert manifest["state"] == "ABORTED"
    assert manifest["complete"] is False
    if kind == "MESSAGE_SIZE":
        assert "safety limit" in manifest["error_message"]
    else:
        assert "suppressed output delivery" in manifest["error_message"]
        assert manifest["outputs"][0]["stream_name"] == "stderr"
        relative_path = manifest["outputs"][0]["representations"][0][
            "relative_path"
        ]
        assert (tmp_path / step.result_manifest_path).parent.joinpath(
            relative_path
        ).read_text() == rate_warning(kind)
    assert {
        "execution.step_completed",
        "execution.operation_completed",
    }.issubset(event_types)
    assert ("execution.completed" in event_types) is not waiting
