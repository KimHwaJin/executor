"""Regression coverage for Artifact evidence created by an interrupted execution cell."""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.commands import (
    CancelExecutionCommand,
    StepSpec,
    SubmitExecutionCommand,
)
from executor_service.application.services import ExecutionService
from executor_service.config import Settings
from executor_service.domain.enums import (
    ArtifactStatus,
    CodeSourceType,
    ExecutionStatus,
    OperationMode,
    RuntimePool,
    RuntimeTargetStatus,
    TriggerType,
)
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.models import ExecutionArtifactORM, RuntimeTargetORM
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.runtime_registry import RuntimeTargetRegistry
from executor_service.infrastructure.worker import ExecutionWorker
from tests.runtime_storage_fake import InMemoryRuntimeStorage


class FileWritingBlockedDriver(InMemoryRuntimeStorage):
    started = asyncio.Event()

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.workspace = ""

    @classmethod
    def configure(cls, _root: Path) -> None:
        cls.reset_storage()
        cls.started = asyncio.Event()

    async def start_session(self, _runtime_profile: str, path: str) -> str:
        self.workspace = path
        return "cancel-artifact-kernel"

    async def execute(self, _runtime_session_id: str, _code: str) -> Any:
        type(self).put_runtime_file(f"{self.workspace}/artifacts/other/cancelled.txt", b"partial")
        type(self).started.set()
        await asyncio.Event().wait()

    async def interrupt_session(self, _runtime_session_id: str) -> None:
        pass

    async def delete_session(self, _runtime_session_id: str) -> None:
        pass

    async def close(self) -> None:
        pass


@pytest.mark.parametrize("mode", [OperationMode.SINGLE, OperationMode.MULTI])
async def test_cancelled_cell_registers_partial_file_as_incomplete_artifact(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: OperationMode,
) -> None:
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        session.add(
            RuntimeTargetORM(
                name="cancel-artifact-runtime",
                connection_config={"endpoint": "http://cancel.invalid:8888"},
                credential_ref="settings:JUPYTER_TOKEN",
                pool=RuntimePool.INTERACTIVE,
                status=RuntimeTargetStatus.ACTIVE,
                max_concurrent_executions=1,
                supported_profiles=["basic"],
                enabled=True,
            )
        )
    code = "write partial file and block"
    execution = await execution_service.submit(
        SubmitExecutionCommand(
            idempotency_key="cancel-artifact-submit",
            operation_mode=mode,
            trigger_type=TriggerType.INTERACTIVE,
            runtime_profile="basic",
            code_source_type=CodeSourceType.INLINE,
            source_content=code,
            code_path=None,
            source_sha256="0" * 64,
            user_id="cancel-artifact-user",
            project_id="cancel-artifact-project",
            session_id="cancel-artifact-session",
            task_id="cancel-artifact-task",
            operation_wait_timeout_seconds=(3600 if mode == OperationMode.MULTI else None),
            steps=(
                StepSpec(
                    sequence=0,
                    code=code,
                    tool_name="write_partial",
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
    FileWritingBlockedDriver.configure(tmp_path)
    monkeypatch.setattr(
        "executor_service.infrastructure.runtime_drivers.JupyterRuntimeDriver",
        FileWritingBlockedDriver,
    )
    task = asyncio.create_task(worker._run_execution(execution.id))
    try:
        await asyncio.wait_for(FileWritingBlockedDriver.started.wait(), timeout=1)
        await execution_service.cancel(
            CancelExecutionCommand(
                execution_id=execution.id,
                idempotency_key="cancel-artifact-command",
                reason="test cancellation",
            )
        )
        task.cancel()
        interrupted = await asyncio.gather(task, return_exceptions=True)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await redis.aclose()

    async with session_factory() as session:
        artifact = await session.scalar(
            select(ExecutionArtifactORM).where(
                ExecutionArtifactORM.execution_id == execution.id,
                ExecutionArtifactORM.name == "cancelled.txt",
            )
        )
    assert artifact is not None
    assert artifact.status == ArtifactStatus.INCOMPLETE
    assert artifact.execution_attempt_id is not None
    assert artifact.execution_step_attempt_id is not None
    assert len(interrupted) == 1
    assert isinstance(interrupted[0], asyncio.CancelledError)

    cancel_requested = await execution_service.get(execution.id)
    assert cancel_requested.status == ExecutionStatus.CANCEL_REQUESTED
    await worker._cancel_execution(execution.id)
    cancelled = await execution_service.get(execution.id)
    assert cancelled.status == ExecutionStatus.CANCELLED
