"""State and event consistency when Runtime-owned storage operations fail."""

from pathlib import Path
from typing import Any, ClassVar

import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.commands import StepSpec, SubmitExecutionCommand
from executor_service.application.services import ExecutionService
from executor_service.config import Settings
from executor_service.domain.enums import (
    AttemptStatus,
    CodeSourceType,
    ExecutionMode,
    ExecutionStatus,
    FailureType,
    OperationStatus,
    RetryStrategy,
    RuntimePool,
    RuntimeSessionCleanupStatus,
    RuntimeTargetStatus,
    StepStatus,
    TriggerType,
)
from executor_service.domain.runtime import RuntimeDriverError, RuntimeExecutionResult
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionOperationORM,
    ExecutionStepORM,
    OutboxEventORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.runtime_registry import RuntimeTargetRegistry
from executor_service.infrastructure.worker import ExecutionWorker
from tests.runtime_storage_fake import InMemoryRuntimeStorage


class FailingRuntimeStorageDriver(InMemoryRuntimeStorage):
    failure_point: ClassVar[str] = ""
    deleted_sessions: ClassVar[list[str]] = []

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    @classmethod
    def configure(cls, failure_point: str) -> None:
        cls.reset_storage()
        cls.failure_point = failure_point
        cls.deleted_sessions = []

    async def prepare_workspace(self, workspace_path: str) -> None:
        if self.failure_point == "prepare_workspace":
            raise RuntimeDriverError("workspace prepare failed")
        await super().prepare_workspace(workspace_path)

    async def start_session(self, _runtime_profile: str, _path: str) -> str:
        return "storage-failure-kernel"

    async def execute(self, _runtime_session_id: str, _code: str) -> RuntimeExecutionResult:
        return RuntimeExecutionResult(
            outputs=[{"output_type": "stream", "name": "stdout", "text": "done\n"}],
            execution_count=1,
        )

    async def write_notebook(self, path: str, notebook: dict[str, Any]) -> None:
        if self.failure_point == "write_notebook":
            raise RuntimeDriverError("notebook write failed")
        await super().write_notebook(path, notebook)

    async def read_manifest(self, workspace_path: str, start: int) -> bytes:
        if self.failure_point == "artifact_discovery":
            raise RuntimeDriverError("artifact discovery failed")
        return await super().read_manifest(workspace_path, start)

    async def interrupt_session(self, _runtime_session_id: str) -> None:
        pass

    async def delete_session(self, runtime_session_id: str) -> None:
        type(self).deleted_sessions.append(runtime_session_id)

    async def close(self) -> None:
        pass


@pytest.mark.parametrize(
    ("failure_point", "expected_step_status", "expected_cleanup", "artifact_event"),
    [
        (
            "prepare_workspace",
            StepStatus.SKIPPED,
            RuntimeSessionCleanupStatus.NOT_REQUIRED,
            False,
        ),
        (
            "write_notebook",
            StepStatus.SUCCEEDED,
            RuntimeSessionCleanupStatus.SUCCEEDED,
            False,
        ),
        (
            "artifact_discovery",
            StepStatus.SUCCEEDED,
            RuntimeSessionCleanupStatus.SUCCEEDED,
            True,
        ),
    ],
)
async def test_runtime_storage_failure_finalizes_consistent_state_and_events(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
    expected_step_status: StepStatus,
    expected_cleanup: RuntimeSessionCleanupStatus,
    artifact_event: bool,
) -> None:
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        session.add(
            RuntimeTargetORM(
                name=f"storage-failure-{failure_point}",
                connection_config={"endpoint": "http://runtime.invalid:8888"},
                credential_ref="settings:JUPYTER_TOKEN",
                pool=RuntimePool.INTERACTIVE,
                status=RuntimeTargetStatus.ACTIVE,
                max_concurrent_executions=1,
                supported_profiles=["basic"],
                enabled=True,
            )
        )
    code = "print('done')"
    execution = await execution_service.submit(
        SubmitExecutionCommand(
            idempotency_key=f"storage-failure-{failure_point}",
            mode=ExecutionMode.STATIC,
            trigger_type=TriggerType.INTERACTIVE,
            runtime_profile="basic",
            code_source_type=CodeSourceType.INLINE,
            source_content=code,
            code_path=None,
            source_sha256="0" * 64,
            user_id="storage-user",
            project_id="storage-project",
            session_id="storage-session",
            task_id="storage-task",
            execution_plan_id="storage-plan",
            steps=(
                StepSpec(
                    sequence=0,
                    code=code,
                    execution_plan_id="storage-plan",
                    plan_step_id="storage-plan-step-0",
                    tool_name="storage_failure_probe",
                ),
            ),
        )
    )
    settings = Settings(runtime_enabled=False, input_host_root=tmp_path)
    redis = Redis.from_url("redis://127.0.0.1:6379/15", decode_responses=True)
    worker = ExecutionWorker(
        session_factory=session_factory,
        redis=redis,
        settings=settings,
        registry=RuntimeTargetRegistry(session_factory, settings),
        artifact_manager=ExecutionArtifactManager(session_factory),
    )
    FailingRuntimeStorageDriver.configure(failure_point)
    monkeypatch.setattr(
        "executor_service.infrastructure.runtime_drivers.JupyterRuntimeDriver",
        FailingRuntimeStorageDriver,
    )
    try:
        await worker._run_execution(execution.id)
    finally:
        await redis.aclose()

    failed = await execution_service.get(execution.id)
    assert failed.status == ExecutionStatus.FAILED
    assert failed.failure_type == FailureType.RUNTIME_UNAVAILABLE
    assert failed.retry_strategy == RetryStrategy.FROM_START
    assert failed.retry_from_sequence == 0
    assert failed.runtime_session_cleanup_status == expected_cleanup

    async with session_factory() as session:
        attempt = await session.scalar(
            select(ExecutionAttemptORM).where(ExecutionAttemptORM.execution_id == execution.id)
        )
        operation = await session.get(ExecutionOperationORM, execution.active_operation_id)
        step = await session.scalar(
            select(ExecutionStepORM).where(ExecutionStepORM.execution_id == execution.id)
        )
        event_types = list(
            await session.scalars(
                select(OutboxEventORM.event_type).where(OutboxEventORM.aggregate_id == execution.id)
            )
        )

    assert attempt is not None
    assert attempt.status == AttemptStatus.FAILED
    assert attempt.failure_type == FailureType.RUNTIME_UNAVAILABLE
    assert attempt.retry_strategy == RetryStrategy.FROM_START
    assert attempt.runtime_session_cleanup_status == expected_cleanup
    assert operation is not None
    assert operation.status == OperationStatus.FAILED
    assert operation.execution_attempt_id == attempt.id
    assert step is not None
    assert step.status == expected_step_status
    assert "execution.operation_failed" in event_types
    assert "execution.failed" in event_types
    assert ("execution.artifact_failed" in event_types) is artifact_event
    assert bool(FailingRuntimeStorageDriver.deleted_sessions) is (
        expected_cleanup == RuntimeSessionCleanupStatus.SUCCEEDED
    )
