"""Fault injection through real Worker, SQLAlchemy and shared result storage."""

import asyncio
import errno
import json
from pathlib import Path
from typing import Any

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
    RuntimeAbortStatus,
    RuntimePool,
    RuntimeSessionCleanupStatus,
    RuntimeTargetStatus,
    StepStatus,
    TriggerType,
)
from executor_service.domain.runtime import (
    RuntimeAbortResult,
    RuntimeDriverError,
    RuntimeExecutionError,
    RuntimeExecutionResult,
    RuntimeOutputHandler,
    RuntimeOutputRecord,
    RuntimeOutputRepresentation,
)
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionOperationORM,
    ExecutionStepAttemptORM,
    ExecutionStepORM,
    OutboxEventORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.diagnostic_store import (
    SQLAlchemyDiagnosticQueryService,
)
from executor_service.infrastructure.execution_worker import ExecutionWorker
from executor_service.infrastructure.result_storage import (
    FilesystemExecutionResultStore,
)
from executor_service.infrastructure.runtime_registry import (
    RuntimeTargetRegistry,
)
from executor_service.interfaces._contracts.diagnostics import (
    ExecutionDiagnosticPageResponse,
)
from executor_service.interfaces._contracts.steps import ExecutionStepResponse
from tests.runtime_credentials import runtime_credential_fields
from tests.runtime_storage_fake import InMemoryRuntimeStorage


class EvidenceDriver(InMemoryRuntimeStorage):
    def __init__(self, fault: str, cleanup_failure: bool) -> None:
        self.reset_storage()
        self.fault = fault
        self.cleanup_failure = cleanup_failure

    async def start_session(self, *_args: object) -> str:
        return "evidence-kernel"

    async def execute_streaming(
        self,
        _session_id: str,
        _code: str,
        handler: RuntimeOutputHandler,
    ) -> RuntimeExecutionResult:
        await handler(
            RuntimeOutputRecord(
                kind="stream",
                stream_name="stdout",
                representations=(
                    RuntimeOutputRepresentation(
                        "text/plain", "UTF8", "partial evidence\n"
                    ),
                ),
            )
        )
        if self.fault == "disconnect":
            try:
                raise ConnectionResetError(
                    errno.ECONNRESET, "connection reset"
                )
            except ConnectionResetError as exc:
                raise RuntimeDriverError(
                    "Jupyter channel unavailable: transport=ConnectionResetError."
                ) from exc
        if self.fault == "timeout":
            await asyncio.sleep(5)
        if self.fault == "tool":
            raise RuntimeExecutionError("ValueError: primary tool failure", [])
        return RuntimeExecutionResult([], 1)

    async def abort_session(self, *_args: object) -> RuntimeAbortResult:
        return RuntimeAbortResult(
            RuntimeAbortStatus.FAILED,
            "Abort idle confirmation deadline expired.",
        )

    async def interrupt_session(self, *_args: object) -> None:
        pass

    async def delete_session(self, *_args: object) -> None:
        if self.cleanup_failure:
            raise RuntimeDriverError(
                "Jupyter REST request failed: method=DELETE status=503."
            )

    async def close(self) -> None:
        pass


@pytest.mark.parametrize("mode", [OperationMode.SINGLE, OperationMode.MULTI])
@pytest.mark.parametrize(
    ("fault", "storage_fault", "cleanup_failure"),
    [
        ("disconnect", None, False),
        ("disconnect", "abort_step_result", True),
        ("success", "append_step_outputs", False),
        ("success", "finalize_step_result", False),
        ("success", "begin_step_result", False),
        ("success", "read_step_projection", False),
        ("timeout", None, True),
        ("timeout", "abort_step_result", False),
        ("timeout", "read_step_projection", True),
        ("tool", "read_step_projection", False),
        ("tool", "finalize_step_result", False),
    ],
)
async def test_failures_preserve_state_reason_partial_refs_and_operator_logs(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    mode: OperationMode,
    fault: str,
    storage_fault: str | None,
    cleanup_failure: bool,
) -> None:
    factory = create_session_factory(engine)
    async with factory() as session, session.begin():
        session.add(
            RuntimeTargetORM(
                name="evidence-runtime",
                connection_config={"endpoint": "http://runtime.invalid"},
                **runtime_credential_fields(),
                pool=RuntimePool.INTERACTIVE,
                status=RuntimeTargetStatus.ACTIVE,
                max_concurrent_executions=1,
                supported_profiles=["basic"],
                enabled=True,
            )
        )
    submitted = await execution_service.submit(
        SubmitExecutionCommand(
            idempotency_key="evidence-execution",
            operation_mode=mode,
            operation_wait_timeout_seconds=600
            if mode == OperationMode.MULTI
            else None,
            trigger_type=TriggerType.INTERACTIVE,
            runtime_profile="basic",
            user_id="evidence-user",
            project_id=None,
            session_id=None,
            task_id="evidence-task",
            steps=(
                StepSpec(
                    sequence=0,
                    code="print('evidence')",
                    step_timeout_seconds=1,
                ),
                StepSpec(sequence=1, code="print('must not run')"),
            ),
        )
    )
    store = FilesystemExecutionResultStore(tmp_path)
    if storage_fault:

        async def fail_storage(*_args: object, **_kwargs: object) -> Any:
            raise PermissionError(
                errno.EACCES, "permission denied", "/secret-private-path"
            )

        monkeypatch.setattr(store, storage_fault, fail_storage)
    driver = EvidenceDriver(fault, cleanup_failure)
    monkeypatch.setattr(
        "executor_service.infrastructure.runtime_drivers.JupyterRuntimeDriver",
        lambda *_args, **_kwargs: driver,
    )
    settings = Settings(runtime_enabled=False, shared_storage_root=tmp_path)
    redis = Redis.from_url("redis://127.0.0.1:6379/15", decode_responses=True)
    worker = ExecutionWorker(
        session_factory=factory,
        redis=redis,
        settings=settings,
        registry=RuntimeTargetRegistry(factory, settings),
        artifact_manager=ExecutionArtifactManager(factory),
        result_store=store,
    )
    try:
        await worker._runner.run(submitted.id)
    finally:
        await redis.aclose()
    execution = await execution_service.get(submitted.id)
    diagnostics = await SQLAlchemyDiagnosticQueryService(factory).list(
        submitted.id
    )
    assert diagnostics.items
    response = ExecutionDiagnosticPageResponse.from_page(diagnostics)
    serialized = response.model_dump_json()
    assert "/secret-private-path" not in serialized
    assert "primary tool failure" not in serialized
    phases = {item.diagnostic.phase for item in diagnostics.items}
    if fault == "disconnect":
        transport = next(
            item
            for item in diagnostics.items
            if item.diagnostic.phase == "RUNTIME_EXECUTE"
        )
        assert transport.diagnostic.code == "RUNTIME_UNAVAILABLE"
        assert any(
            cause.errno == errno.ECONNRESET
            for cause in transport.diagnostic.causes
        )
    if fault == "timeout":
        assert "RUNTIME_TIMEOUT" in phases
    if storage_fault == "read_step_projection":
        assert "NOTEBOOK_BUILD" in phases
    if storage_fault in {"abort_step_result", "finalize_step_result"}:
        assert phases & {"RESULT_FINALIZE", "RESULT_FAILURE_SAVE"}
    if cleanup_failure:
        assert any(
            item.diagnostic.category == "CLEANUP" for item in diagnostics.items
        )
    waiting = (
        mode == OperationMode.MULTI
        and fault == "tool"
        and storage_fault == "read_step_projection"
    )
    assert execution.status == (
        ExecutionStatus.WAITING_FOR_OPERATION
        if waiting
        else ExecutionStatus.FAILED
    )
    expected_failure = (
        FailureType.RUNTIME_UNAVAILABLE
        if fault == "disconnect"
        else FailureType.STEP_TIMEOUT
        if fault == "timeout"
        else FailureType.TOOL_ERROR
        if fault == "tool"
        else FailureType.COMPLETION_FAILED
        if storage_fault == "read_step_projection"
        else FailureType.INTERNAL_ERROR
    )
    assert execution.failure_type == (None if waiting else expected_failure)
    if not waiting:
        assert execution.error_message is not None
        expected_message = {
            "disconnect": "ConnectionResetError",
            "timeout": "Step timeout",
            "tool": "primary tool failure",
            "success": (
                "NOTEBOOK_BUILD"
                if storage_fault == "read_step_projection"
                else "errno=13"
            ),
        }[fault]
        assert expected_message in execution.error_message
    if cleanup_failure:
        assert (
            execution.runtime_session_cleanup_status
            == RuntimeSessionCleanupStatus.FAILED
        )
        assert "status=503" not in (execution.error_message or "")

    async with factory() as session:
        attempt = await session.scalar(
            select(ExecutionAttemptORM).where(
                ExecutionAttemptORM.execution_id == submitted.id
            )
        )
        steps = list(
            await session.scalars(
                select(ExecutionStepORM)
                .where(ExecutionStepORM.execution_id == submitted.id)
                .order_by(ExecutionStepORM.sequence)
            )
        )
        history = await session.scalar(
            select(ExecutionStepAttemptORM).where(
                ExecutionStepAttemptORM.execution_id == submitted.id,
                ExecutionStepAttemptORM.sequence == 0,
            )
        )
        operation = await session.get(
            ExecutionOperationORM, submitted.active_operation_id
        )
        events = list(
            await session.scalars(
                select(OutboxEventORM).where(
                    OutboxEventORM.aggregate_id == submitted.id
                )
            )
        )
    assert attempt is not None and attempt.failure_type == (
        None if waiting else expected_failure
    )
    assert operation is not None and operation.status == OperationStatus.FAILED
    assert history is not None
    assert steps[1].status == StepStatus.SKIPPED
    assert steps[0].status == (
        StepStatus.SUCCEEDED
        if storage_fault == "read_step_projection" and fault == "success"
        else StepStatus.FAILED
    )
    assert history.status == steps[0].status
    step_events = [
        event
        for event in events
        if event.event_type == "execution.step_completed"
    ]
    assert len(step_events) == 1
    assert len(
        [
            event
            for event in events
            if event.event_type == "execution.completed"
        ]
    ) == (0 if waiting else 1)

    if storage_fault in {"abort_step_result", "begin_step_result"}:
        assert steps[0].result_manifest_path is None
    else:
        assert steps[0].result_manifest_path is not None
        assert history.result_manifest_path == steps[0].result_manifest_path
        manifest = json.loads(
            (tmp_path / steps[0].result_manifest_path).read_text()
        )
        expected_complete = (
            storage_fault == "read_step_projection" and fault != "timeout"
        )
        assert manifest["complete"] is expected_complete
        if storage_fault != "append_step_outputs":
            output = manifest["outputs"][0]["representations"][0]
            assert (tmp_path / steps[0].result_manifest_path).parent.joinpath(
                output["relative_path"]
            ).read_text() == "partial evidence\n"
        response = ExecutionStepResponse.from_domain(
            execution.steps[0], execution.id
        )
        assert response.result.result_ref is not None
        assert response.result.result_ref.complete is expected_complete
        if fault == "timeout":
            assert "Step timeout" in manifest["error_message"]
        if storage_fault == "read_step_projection":
            assert execution.notebook_projection_status == "FAILED"
            assert "errno=13" in (execution.notebook_projection_error or "")
            if fault == "tool":
                assert "primary tool failure" in (steps[0].error_message or "")
                assert "primary tool failure" in (
                    operation.error_message or ""
                )
    diagnostics = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.getMessage().startswith('{"event": "runtime.failure"')
    ]
    assert diagnostics
    assert all(
        item["execution_id"] == str(execution.id) for item in diagnostics
    )
    if fault == "disconnect":
        assert any(
            error["type"] == "ConnectionResetError"
            for item in diagnostics
            for error in item["errors"]
        )
    if cleanup_failure:
        assert any(
            item["phase"] in {"RUNTIME_DELETE", "RUNTIME_DELETE_AFTER_ABORT"}
            for item in diagnostics
        )
    if storage_fault == "abort_step_result":
        assert any(
            item["phase"] == "RESULT_FAILURE_SAVE" for item in diagnostics
        )
    assert "secret-private-path" not in caplog.text
