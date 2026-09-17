"""A saved notebook is a live Execution file, not successful Step evidence."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import ClassVar
from uuid import UUID, uuid4

import nbformat
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.commands import (
    CancelExecutionCommand,
    CreateOperationCommand,
    FinalizeExecutionCommand,
    RetryExecutionCommand,
    StepSpec,
)
from executor_service.application.services import ExecutionService
from executor_service.domain.enums import (
    ArtifactStatus,
    ArtifactType,
    ExecutionStatus,
    OperationMode,
)
from executor_service.domain.runtime import (
    RuntimeDriverError,
    RuntimeExecutionResult,
    RuntimeNotebookPreparationResult,
    RuntimeNotebookSourceCell,
)
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.models import (
    ExecutionArtifactORM,
    ExecutionStepAttemptORM,
)
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.execution_leases import (
    ExecutionLeaseLostError,
)
from executor_service.infrastructure.workspace import WorkspaceManager
from tests.runtime_storage_fake import InMemoryRuntimeStorage
from tests.test_artifacts import _command, _seed_attempt
from tests.test_multi_lifecycle import (
    RecordingMultiDriver,
    _multi_command,
    _patch_runtime_driver,
    _worker,
)
from tests.test_required_result_completion import setup_runtime


async def test_notebook_identity_is_not_a_step_or_content_snapshot(
    execution_service: ExecutionService, engine: AsyncEngine
) -> None:
    execution = await execution_service.submit(_command())
    _, lease = await _seed_attempt(engine, execution.id, execution.steps[0].id)
    factory = create_session_factory(engine)
    async with factory() as session, session.begin():
        await session.execute(delete(ExecutionStepAttemptORM))
    workspace = WorkspaceManager().plan(execution)
    driver = InMemoryRuntimeStorage()
    driver.reset_storage()
    manager = ExecutionArtifactManager(factory)
    with pytest.raises(KeyError):
        await manager.register_notebook(
            driver=driver, workspace=workspace, lease=lease
        )
    async with factory() as session:
        assert not list(await session.scalars(select(ExecutionArtifactORM)))
    await driver.write_notebook(workspace.notebook_path, {"cells": []})
    first = await manager.register_notebook(
        driver=driver, workspace=workspace, lease=lease
    )
    async with factory() as session:
        initial = await session.get(ExecutionArtifactORM, first[0])
        assert initial is not None
        created_at = initial.created_at
        old_checksum = initial.checksum_sha256
    await driver.write_notebook(
        workspace.notebook_path, {"cells": [{"source": "new source"}]}
    )
    assert (
        await manager.register_notebook(
            driver=driver, workspace=workspace, lease=lease
        )
        == first
    )
    with pytest.raises(ExecutionLeaseLostError):
        await manager.register_notebook(
            driver=driver,
            workspace=workspace,
            lease=replace(lease, fencing_token=lease.fencing_token + 1),
        )
    await execution_service.cancel(
        CancelExecutionCommand(execution.id, uuid4().hex)
    )
    assert (
        await manager.register_notebook(
            driver=driver, workspace=workspace, lease=lease
        )
        == first
    )
    async with factory() as session:
        rows = list(await session.scalars(select(ExecutionArtifactORM)))
    assert len(rows) == 1
    artifact = rows[0]
    assert artifact.status == ArtifactStatus.AVAILABLE
    assert artifact.created_at == created_at
    assert artifact.checksum_sha256 != old_checksum
    assert artifact.execution_attempt_id is None
    assert artifact.execution_step_id is None
    assert artifact.execution_step_attempt_id is None
    assert artifact.artifact_metadata["content_semantics"] == "CURRENT_FILE"


class LiveNotebookDriver(RecordingMultiDriver):
    started: ClassVar[asyncio.Event]
    proceed: ClassVar[asyncio.Event]
    fail_prepare: ClassVar[bool] = False
    fail_project: ClassVar[bool] = False

    async def prepare_notebook(
        self,
        workspace_path: str,
        execution_id: UUID,
        runtime_profile: str,
        cells: tuple[RuntimeNotebookSourceCell, ...],
    ) -> RuntimeNotebookPreparationResult:
        if self.fail_prepare:
            raise RuntimeDriverError("prepare unavailable")
        path = f"{workspace_path}/notebooks/execution.ipynb"
        document = nbformat.v4.new_notebook(
            cells=[nbformat.v4.new_code_cell(cell.source) for cell in cells]
        )
        await InMemoryRuntimeStorage.write_notebook(self, path, document)
        return RuntimeNotebookPreparationResult(path, len(cells), len(cells))

    async def write_notebook(self, path: str, notebook: dict) -> None:
        if self.fail_project:
            raise RuntimeDriverError("projection unavailable")
        await super().write_notebook(path, notebook)

    async def execute(
        self, session_id: str, code: str
    ) -> RuntimeExecutionResult:
        self.started.set()
        await self.proceed.wait()
        return await super().execute(session_id, code)


async def _live_runtime(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_runtime(engine, monkeypatch)
    LiveNotebookDriver.reset_storage()
    LiveNotebookDriver.executed = []
    LiveNotebookDriver.deleted = []
    LiveNotebookDriver.fail_code = None
    LiveNotebookDriver.slow_code = None
    LiveNotebookDriver.limit_code = None
    LiveNotebookDriver.delete_fails = False
    LiveNotebookDriver.fail_prepare = False
    LiveNotebookDriver.fail_project = False
    LiveNotebookDriver.started = asyncio.Event()
    LiveNotebookDriver.proceed = asyncio.Event()
    _patch_runtime_driver(monkeypatch, LiveNotebookDriver)


async def _notebook(
    engine: AsyncEngine, execution_id: UUID
) -> ExecutionArtifactORM:
    async with create_session_factory(engine)() as session:
        rows = list(
            await session.scalars(
                select(ExecutionArtifactORM).where(
                    ExecutionArtifactORM.execution_id == execution_id,
                    ExecutionArtifactORM.artifact_type
                    == ArtifactType.NOTEBOOK,
                )
            )
        )
    assert len(rows) == 1
    assert rows[0].status == ArtifactStatus.AVAILABLE
    return rows[0]


@pytest.mark.parametrize("mode", [OperationMode.SINGLE, OperationMode.MULTI])
@pytest.mark.parametrize("outcome", ["success", "error", "timeout", "cancel"])
async def test_notebook_available_during_run_failure_retry_and_finalize(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: OperationMode,
    outcome: str,
) -> None:
    await _live_runtime(engine, monkeypatch)
    if outcome == "error":
        LiveNotebookDriver.fail_code = "value = 1"
    if outcome == "timeout":
        LiveNotebookDriver.slow_code = "value = 1"
    command = replace(
        _multi_command(uuid4().hex),
        operation_mode=mode,
        operation_wait_timeout_seconds=600
        if mode == OperationMode.MULTI
        else None,
        steps=(
            StepSpec(
                0,
                "value = 1",
                step_timeout_seconds=1 if outcome == "timeout" else None,
            ),
        ),
    )
    execution = await execution_service.submit(command)
    worker, redis = _worker(engine, tmp_path)
    task = asyncio.create_task(worker._runner.run(execution.id))
    try:
        await asyncio.wait_for(LiveNotebookDriver.started.wait(), 5)
        original = await _notebook(engine, execution.id)
        assert original.relative_path is not None
        original_body = LiveNotebookDriver.files[original.relative_path][0]
        nbformat.validate(nbformat.reads(original_body.decode(), as_version=4))
        assert not json.loads(original_body)["cells"][0]["outputs"]
        assert original.execution_step_id is None
        if outcome == "cancel":
            await execution_service.cancel(
                CancelExecutionCommand(execution.id, uuid4().hex)
            )
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await worker._cancellation.cancel(execution.id)
            assert (await execution_service.get(execution.id)).status == (
                ExecutionStatus.CANCELLED
            )
        else:
            LiveNotebookDriver.proceed.set()
            await asyncio.wait_for(task, 5)
            observed = await execution_service.get(execution.id)
            current = await _notebook(engine, execution.id)
            assert current.id == original.id
            document = json.loads(
                LiveNotebookDriver.files[original.relative_path][0]
            )
            nbformat.validate(nbformat.from_dict(document))
            if outcome != "timeout":
                assert document["cells"][0]["outputs"]
            if outcome == "error":
                assert document["cells"][0]["outputs"][0]["output_type"] == (
                    "error"
                )
            if outcome in {"error", "timeout"}:
                LiveNotebookDriver.fail_code = None
                LiveNotebookDriver.slow_code = None
                if mode == OperationMode.SINGLE:
                    assert observed.status == ExecutionStatus.FAILED
                    await execution_service.retry(
                        RetryExecutionCommand(execution.id, uuid4().hex)
                    )
                else:
                    await execution_service.create_operation(
                        CreateOperationCommand(
                            execution_id=execution.id,
                            idempotency_key=uuid4().hex,
                            expected_version=observed.version,
                            steps=(StepSpec(1, "corrected = 2"),),
                        )
                    )
                await worker._runner.run(execution.id)
            if mode == OperationMode.MULTI:
                waiting = await execution_service.get(execution.id)
                assert waiting.status == ExecutionStatus.WAITING_FOR_OPERATION
                await execution_service.finalize_execution(
                    FinalizeExecutionCommand(
                        execution.id, uuid4().hex, waiting.version
                    )
                )
                await worker._runner.run(execution.id)
            assert (await execution_service.get(execution.id)).status == (
                ExecutionStatus.SUCCEEDED
            )
        latest = await _notebook(engine, execution.id)
        assert latest.id == original.id
        assert latest.created_at == original.created_at
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await redis.aclose()


@pytest.mark.parametrize("failure", ["prepare", "project"])
async def test_failed_write_never_exposes_an_unsaved_notebook(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    await _live_runtime(engine, monkeypatch)
    LiveNotebookDriver.fail_prepare = failure == "prepare"
    LiveNotebookDriver.fail_project = failure == "project"
    LiveNotebookDriver.proceed.set()
    execution = await execution_service.submit(_multi_command(uuid4().hex))
    worker, redis = _worker(engine, tmp_path)
    try:
        await worker._runner.run(execution.id)
        assert (await execution_service.get(execution.id)).status == (
            ExecutionStatus.FAILED
        )
        if failure == "prepare":
            async with create_session_factory(engine)() as session:
                assert not list(
                    await session.scalars(select(ExecutionArtifactORM))
                )
        else:
            # A failed replacement does not invalidate the last saved file.
            artifact = await _notebook(engine, execution.id)
            assert artifact.relative_path is not None
            body = LiveNotebookDriver.files[artifact.relative_path][0]
            nbformat.validate(nbformat.reads(body.decode(), as_version=4))
    finally:
        await redis.aclose()
