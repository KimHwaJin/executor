"""Post-code delivery failures cannot emit success or authorize code replay."""

from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.commands import (
    CreateOperationCommand,
    FinalizeExecutionCommand,
    RetryExecutionCommand,
    StepSpec,
)
from executor_service.application.services import ExecutionService
from executor_service.domain.enums import (
    ArtifactType,
    ExecutionStatus,
    FailureType,
    OperationMode,
    OperationStatus,
    RetryStrategy,
    RuntimePool,
    RuntimeTargetStatus,
    StepStatus,
)
from executor_service.domain.errors import InvalidStateTransitionError
from executor_service.domain.runtime import (
    RuntimeDriverError,
    RuntimeFileMetadata,
)
from executor_service.infrastructure.db.models import (
    ExecutionArtifactORM,
    ExecutionEventORM,
    ExecutionOperationORM,
    ExecutionORM,
    ExecutionStepORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.diagnostic_store import (
    SQLAlchemyDiagnosticQueryService,
)
from tests.runtime_credentials import runtime_credential_fields
from tests.test_multi_lifecycle import (
    RecordingMultiDriver,
    _multi_command,
    _patch_runtime_driver,
    _worker,
)


class CompletionDriver(RecordingMultiDriver):
    fault: ClassVar[str] = ""
    writes: ClassVar[int] = 0

    async def write_notebook(
        self, path: str, notebook: dict[str, Any]
    ) -> None:
        type(self).writes += 1
        if self.fault == "write" or (
            self.fault == "transient" and self.writes < 3
        ):
            raise RuntimeDriverError("injected notebook storage outage")
        await super().write_notebook(path, notebook)

    async def file_metadata(self, path: str) -> RuntimeFileMetadata:
        if self.fault == "catalog":
            raise RuntimeDriverError("injected notebook metadata outage")
        return await super().file_metadata(path)

    async def read_manifest(self, workspace_path: str, start: int) -> bytes:
        if self.fault == "artifacts":
            raise RuntimeDriverError("injected artifact discovery outage")
        return await super().read_manifest(workspace_path, start)


async def setup_runtime(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    CompletionDriver.reset_storage()
    CompletionDriver.executed = []
    CompletionDriver.deleted = []
    CompletionDriver.delete_fails = False
    CompletionDriver.fail_code = None
    CompletionDriver.slow_code = None
    CompletionDriver.limit_code = None
    CompletionDriver.fault = ""
    CompletionDriver.writes = 0
    _patch_runtime_driver(monkeypatch, CompletionDriver)
    factory = create_session_factory(engine)
    async with factory() as session, session.begin():
        session.add(
            RuntimeTargetORM(
                name=f"completion-{uuid4()}",
                connection_config={"endpoint": "http://fake-jupyter"},
                **runtime_credential_fields(),
                pool=RuntimePool.INTERACTIVE,
                status=RuntimeTargetStatus.ACTIVE,
                max_concurrent_executions=2,
                supported_profiles=["basic"],
                enabled=True,
            )
        )


@pytest.mark.parametrize("mode", [OperationMode.SINGLE, OperationMode.MULTI])
@pytest.mark.parametrize(
    "fault", ["write", "catalog", "artifacts", "transient", "cleanup"]
)
async def test_required_delivery_and_retry_policy(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: OperationMode,
    fault: str,
) -> None:
    await setup_runtime(engine, monkeypatch)
    CompletionDriver.fault = fault
    worker, redis = _worker(engine, tmp_path)
    command = replace(
        _multi_command(str(uuid4())),
        operation_mode=mode,
        operation_wait_timeout_seconds=600
        if mode == OperationMode.MULTI
        else None,
        steps=(StepSpec(0, "value = 1"), StepSpec(1, "print(value)")),
    )
    execution = await execution_service.submit(command)
    try:
        # MULTI releases its session only during explicit finalization.
        CompletionDriver.delete_fails = (
            fault == "cleanup" and mode == OperationMode.SINGLE
        )
        await worker._runner.run(execution.id)
        if fault in {"cleanup", "catalog"} and mode == OperationMode.MULTI:
            waiting = await execution_service.get(execution.id)
            assert waiting.status == ExecutionStatus.WAITING_FOR_OPERATION
            await execution_service.finalize_execution(
                FinalizeExecutionCommand(
                    execution_id=execution.id,
                    idempotency_key=str(uuid4()),
                    expected_version=waiting.version,
                )
            )
            CompletionDriver.delete_fails = fault == "cleanup"
            await worker._runner.run(execution.id)
        finished = await execution_service.get(execution.id)
        factory = create_session_factory(engine)
        async with factory() as session:
            operation = await session.get(
                ExecutionOperationORM, execution.active_operation_id
            )
            events = list(
                await session.scalars(
                    select(ExecutionEventORM).where(
                        ExecutionEventORM.execution_id == execution.id,
                        ExecutionEventORM.event_type.in_(
                            [
                                "execution.operation_completed",
                                "execution.completed",
                            ]
                        ),
                    )
                )
            )
        assert operation is not None
        if fault == "transient":
            assert finished.status == (
                ExecutionStatus.SUCCEEDED
                if mode == OperationMode.SINGLE
                else ExecutionStatus.WAITING_FOR_OPERATION
            )
            assert CompletionDriver.executed == ["value = 1", "print(value)"]
            assert CompletionDriver.writes == 4
            assert operation.status == OperationStatus.SUCCEEDED
            return
        assert finished.status == ExecutionStatus.FAILED
        assert finished.failure_type == FailureType.COMPLETION_FAILED
        assert finished.retry_strategy == RetryStrategy.NOT_RETRYABLE
        assert finished.steps[0].status == StepStatus.SUCCEEDED
        assert finished.steps[0].result_complete is True
        ref = finished.steps[0].result_manifest_path
        assert ref is not None and (tmp_path / ref).is_file()
        early_failure = fault in {"write", "artifacts"}
        assert finished.steps[1].status == (
            StepStatus.SKIPPED if early_failure else StepStatus.SUCCEEDED
        )
        assert len(CompletionDriver.executed) == (1 if early_failure else 2)
        if fault in {"cleanup", "catalog"} and mode == OperationMode.MULTI:
            # A previously published successful Operation stays immutable.
            assert operation.status == OperationStatus.SUCCEEDED
        else:
            assert operation.status == OperationStatus.FAILED
            assert all(event.payload["status"] == "FAILED" for event in events)
            op_event = next(
                event
                for event in events
                if event.event_type == "execution.operation_completed"
            )
            assert (
                op_event.payload["error"]["code"]
                == "OPERATION_COMPLETION_FAILED"
            )
        terminal = [
            event
            for event in events
            if event.event_type == "execution.completed"
        ]
        assert len(terminal) == 1
        assert terminal[0].payload["status"] == "FAILED"
        with pytest.raises(InvalidStateTransitionError):
            await execution_service.retry(
                RetryExecutionCommand(
                    execution_id=execution.id,
                    idempotency_key=str(uuid4()),
                )
            )
        diagnostics = await SQLAlchemyDiagnosticQueryService(factory).list(
            execution.id
        )
        assert any(
            item.diagnostic.code == "COMPLETION_FAILED"
            for item in diagnostics.items
        )
    finally:
        await redis.aclose()


async def test_multi_failed_history_requires_corrective_operation_before_finalize(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await setup_runtime(engine, monkeypatch)
    CompletionDriver.fail_code = "value = 1"
    worker, redis = _worker(engine, tmp_path)
    execution = await execution_service.submit(_multi_command(str(uuid4())))
    try:
        await worker._runner.run(execution.id)
        waiting = await execution_service.get(execution.id)
        assert waiting.status == ExecutionStatus.WAITING_FOR_OPERATION
        with pytest.raises(
            InvalidStateTransitionError, match="successful last Operation"
        ):
            await execution_service.finalize_execution(
                FinalizeExecutionCommand(
                    execution_id=execution.id,
                    idempotency_key=str(uuid4()),
                    expected_version=waiting.version,
                )
            )
        await execution_service.create_operation(
            CreateOperationCommand(
                execution_id=execution.id,
                idempotency_key=str(uuid4()),
                expected_version=waiting.version,
                steps=(StepSpec(1, "value = 2"),),
            )
        )
        await worker._runner.run(execution.id)
        waiting = await execution_service.get(execution.id)
        assert waiting.status == ExecutionStatus.WAITING_FOR_OPERATION
        await execution_service.finalize_execution(
            FinalizeExecutionCommand(
                execution_id=execution.id,
                idempotency_key=str(uuid4()),
                expected_version=waiting.version,
            )
        )
        await worker._runner.run(execution.id)
        finished = await execution_service.get(execution.id)
        assert finished.status == ExecutionStatus.SUCCEEDED
        assert [step.status for step in finished.steps] == [
            StepStatus.FAILED,
            StepStatus.SUCCEEDED,
        ]
        assert CompletionDriver.executed == ["value = 1", "value = 2"]
    finally:
        await redis.aclose()


@pytest.mark.parametrize(
    "corruption",
    ["missing_manifest", "incomplete_ref", "unregistered_notebook"],
)
async def test_multi_finalize_rechecks_required_results_without_code_replay(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
) -> None:
    await setup_runtime(engine, monkeypatch)
    worker, redis = _worker(engine, tmp_path)
    execution = await execution_service.submit(_multi_command(str(uuid4())))
    try:
        await worker._runner.run(execution.id)
        waiting = await execution_service.get(execution.id)
        assert waiting.status == ExecutionStatus.WAITING_FOR_OPERATION
        if corruption == "missing_manifest":
            ref = waiting.steps[0].result_manifest_path
            assert ref is not None
            (tmp_path / ref).unlink()
        elif corruption == "incomplete_ref":
            factory = create_session_factory(engine)
            async with factory() as session, session.begin():
                step = await session.get(ExecutionStepORM, waiting.steps[0].id)
                assert step is not None
                step.result_complete = False
        else:
            factory = create_session_factory(engine)
            async with factory() as session, session.begin():
                for artifact in await session.scalars(
                    select(ExecutionArtifactORM).where(
                        ExecutionArtifactORM.execution_id == execution.id,
                        ExecutionArtifactORM.artifact_type
                        == ArtifactType.NOTEBOOK,
                    )
                ):
                    await session.delete(artifact)

            async def silently_skip(**_kwargs: Any) -> None:
                pass

            monkeypatch.setattr(
                worker._notebook_projector,
                "register_artifact",
                silently_skip,
            )
        await execution_service.finalize_execution(
            FinalizeExecutionCommand(
                execution_id=execution.id,
                idempotency_key=str(uuid4()),
                expected_version=waiting.version,
            )
        )
        await worker._runner.run(execution.id)
        finished = await execution_service.get(execution.id)
        assert finished.status == ExecutionStatus.FAILED
        assert finished.failure_type == FailureType.COMPLETION_FAILED
        assert finished.retry_strategy == RetryStrategy.NOT_RETRYABLE
        assert CompletionDriver.executed == ["value = 1"]
        async with create_session_factory(engine)() as session:
            row = await session.get(ExecutionORM, execution.id)
            assert row is not None and row.lease_owner is None
    finally:
        await redis.aclose()
