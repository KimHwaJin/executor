"""Worker consistency when one Runtime output message exceeds its limit."""

import json
from pathlib import Path
from typing import Any, cast

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
)
from executor_service.infrastructure.artifacts import (
    ExecutionArtifactManager,
)
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
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


class OutputLimitRuntimeDriver(InMemoryRuntimeStorage):
    def __init__(self, max_message_bytes: int) -> None:
        self.max_message_bytes = max_message_bytes

    async def start_session(
        self, _runtime_profile: str, _working_directory: str
    ) -> str:
        return "output-limit-kernel"

    async def execute(self, _session_id: str, _code: str) -> Any:
        raise RuntimeOutputLimitExceededError(self.max_message_bytes)

    async def abort_session(
        self, _session_id: str, _timeout_seconds: float
    ) -> RuntimeAbortResult:
        return RuntimeAbortResult(RuntimeAbortStatus.IDLE_CONFIRMED)

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


async def test_output_limit_aborts_runtime_and_persists_incomplete_result(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
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
            operation_mode=OperationMode.SINGLE,
            trigger_type=TriggerType.INTERACTIVE,
            runtime_profile="basic",
            user_id="output-user",
            project_id=None,
            session_id=None,
            task_id="output-task",
            steps=(StepSpec(sequence=0, code="print('large')"),),
        )
    )
    settings = Settings(
        runtime_enabled=False,
        shared_storage_root=tmp_path,
        runtime_max_output_message_bytes=1048576,
    )
    redis = Redis.from_url("redis://127.0.0.1:6379/15", decode_responses=True)
    driver = OutputLimitRuntimeDriver(
        settings.runtime_max_output_message_bytes
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
        await worker._run_execution(execution.id)
    finally:
        await redis.aclose()

    finished = await execution_service.get(execution.id)
    assert finished.status == ExecutionStatus.FAILED
    assert finished.failure_type == FailureType.OUTPUT_LIMIT_EXCEEDED
    assert finished.retry_strategy == RetryStrategy.FROM_FAILED_STEP
    assert finished.runtime_abort_status == RuntimeAbortStatus.IDLE_CONFIRMED
    assert (
        finished.runtime_session_cleanup_status
        == RuntimeSessionCleanupStatus.NOT_REQUIRED
    )

    async with session_factory() as session:
        attempt = await session.scalar(
            select(ExecutionAttemptORM).where(
                ExecutionAttemptORM.execution_id == execution.id
            )
        )
        step = await session.scalar(
            select(ExecutionStepORM).where(
                ExecutionStepORM.execution_id == execution.id
            )
        )
        event_types = set(
            await session.scalars(
                select(OutboxEventORM.event_type).where(
                    OutboxEventORM.aggregate_id == execution.id
                )
            )
        )

    assert attempt is not None
    assert attempt.failure_type == FailureType.OUTPUT_LIMIT_EXCEEDED
    assert step is not None
    assert step.status == StepStatus.FAILED
    assert step.result_complete is False
    assert step.result_manifest_path is not None
    manifest = json.loads((tmp_path / step.result_manifest_path).read_bytes())
    assert manifest["state"] == "ABORTED"
    assert manifest["complete"] is False
    assert "safety limit" in manifest["error_message"]
    assert {
        "execution.step_completed",
        "execution.operation_completed",
        "execution.completed",
    }.issubset(event_types)
