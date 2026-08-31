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
    ExecutionStatus,
    OperationMode,
    RuntimePool,
    RuntimeTargetStatus,
    TriggerType,
)
from executor_service.domain.runtime import (
    RuntimeOutputHandler,
    RuntimeOutputRecord,
    RuntimeOutputRepresentation,
)
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.models import (
    ExecutionArtifactORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.diagnostic_store import (
    SQLAlchemyDiagnosticQueryService,
)
from executor_service.infrastructure.execution_worker import ExecutionWorker
from executor_service.infrastructure.runtime_registry import (
    RuntimeTargetRegistry,
)
from tests.result_evidence_assertions import assert_result_evidence_surfaces
from tests.runtime_credentials import runtime_credential_fields
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
        type(self).put_runtime_file(
            f"{self.workspace}/artifacts/other/cancelled.txt", b"partial"
        )
        type(self).started.set()
        await asyncio.Event().wait()

    async def execute_streaming(
        self, session_id: str, code: str, handler: RuntimeOutputHandler
    ) -> Any:
        await handler(
            RuntimeOutputRecord(
                kind="stream",
                stream_name="stdout",
                representations=(
                    RuntimeOutputRepresentation(
                        "text/plain", "UTF8", "before cancellation\n"
                    ),
                ),
            )
        )
        return await self.execute(session_id, code)

    async def interrupt_session(self, _runtime_session_id: str) -> None:
        pass

    async def delete_session(self, _runtime_session_id: str) -> None:
        pass

    async def close(self) -> None:
        pass


@pytest.mark.parametrize("mode", [OperationMode.SINGLE, OperationMode.MULTI])
@pytest.mark.parametrize(
    "result_fault", [None, "seal", "reference", "reference_timeout"]
)
@pytest.mark.parametrize("step_timeout", [None, 600])
async def test_cancelled_cell_registers_partial_file_as_incomplete_artifact(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: OperationMode,
    result_fault: str | None,
    step_timeout: int | None,
) -> None:
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        session.add(
            RuntimeTargetORM(
                name="cancel-artifact-runtime",
                connection_config={"endpoint": "http://cancel.invalid:8888"},
                **runtime_credential_fields(),
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
            user_id="cancel-artifact-user",
            project_id="cancel-artifact-project",
            session_id="cancel-artifact-session",
            task_id="cancel-artifact-task",
            operation_wait_timeout_seconds=(
                3600 if mode == OperationMode.MULTI else None
            ),
            steps=(
                StepSpec(
                    sequence=0,
                    code=code,
                    tool_name="write_partial",
                    step_timeout_seconds=step_timeout,
                ),
                StepSpec(sequence=1, code="print('must not run')"),
            ),
        )
    )
    settings = Settings(runtime_enabled=False, shared_storage_root=tmp_path)
    redis = Redis.from_url("redis://127.0.0.1:6379/15", decode_responses=True)
    worker = ExecutionWorker(
        session_factory=session_factory,
        redis=redis,
        settings=settings,
        registry=RuntimeTargetRegistry(session_factory, settings),
        artifact_manager=ExecutionArtifactManager(session_factory),
    )
    FileWritingBlockedDriver.configure(tmp_path)
    if result_fault == "seal":

        async def fail_seal(*_args: object, **_kwargs: object) -> Any:
            raise PermissionError("cannot seal cancelled evidence")

        monkeypatch.setattr(
            worker._result_store, "abort_step_result", fail_seal
        )
    elif result_fault is not None:

        async def fail_reference(*_args: object, **_kwargs: object) -> None:
            if result_fault == "reference_timeout":
                await asyncio.sleep(30)
            raise PermissionError("cannot attach cancelled reference")

        monkeypatch.setattr(
            worker._step_executor, "_record_interrupted_result", fail_reference
        )
    monkeypatch.setattr(
        "executor_service.infrastructure.runtime_drivers.JupyterRuntimeDriver",
        FileWritingBlockedDriver,
    )
    task = asyncio.create_task(worker._runner.run(execution.id))
    try:
        await asyncio.wait_for(
            FileWritingBlockedDriver.started.wait(), timeout=1
        )
        await execution_service.cancel(
            CancelExecutionCommand(
                execution_id=execution.id,
                idempotency_key="cancel-artifact-command",
                reason="test cancellation",
            )
        )
        task.cancel()
        async with asyncio.timeout(5):
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
    await worker._cancellation.cancel(execution.id)
    cancelled = await execution_service.get(execution.id)
    assert cancelled.status == ExecutionStatus.CANCELLED
    assert cancelled.steps[0].result_complete is (
        None if result_fault else False
    )
    if result_fault:
        diagnostics = await SQLAlchemyDiagnosticQueryService(
            session_factory
        ).list(execution.id)
        assert any(
            item.diagnostic.phase
            == (
                "RESULT_FAILURE_SAVE"
                if result_fault == "seal"
                else "RESULT_REFERENCE_PERSIST"
            )
            for item in diagnostics.items
        )
    assert cancelled.steps[1].result_manifest_path is None
    await assert_result_evidence_surfaces(
        session_factory, execution.id, tmp_path
    )
