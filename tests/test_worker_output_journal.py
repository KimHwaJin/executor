import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, ClassVar, cast
from uuid import UUID, uuid5

import pytest

from executor_service.domain.runtime import (
    RuntimeDriverError,
    RuntimeExecutionError,
    RuntimeExecutionResult,
    RuntimeNotebookCell,
    RuntimeNotebookMaterializationResult,
    RuntimeNotebookPreparationResult,
    RuntimeNotebookSourceCell,
    RuntimeOutputAppendResult,
    RuntimeOutputDescriptor,
    RuntimeOutputHandler,
    RuntimeOutputJournalDescriptor,
    RuntimeOutputJournalIdentity,
    RuntimeOutputRecord,
    RuntimeOutputRepresentation,
    RuntimeOutputRepresentationDescriptor,
)
from executor_service.infrastructure.worker import ExecutionWorker

JOURNAL_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


def _identity() -> RuntimeOutputJournalIdentity:
    return RuntimeOutputJournalIdentity(
        workspace_path="users/u/projects/p/sessions/s/executions/e",
        execution_id=UUID("11111111-1111-4111-8111-111111111111"),
        operation_id=UUID("22222222-2222-4222-8222-222222222222"),
        step_id=UUID("33333333-3333-4333-8333-333333333333"),
        sequence=0,
        execution_attempt_id=UUID("44444444-4444-4444-8444-444444444444"),
        fencing_token=9,
        runtime_target_id=UUID("55555555-5555-4555-8555-555555555555"),
        runtime_session_id="kernel-1",
    )


def _record(value: str) -> RuntimeOutputRecord:
    return RuntimeOutputRecord(
        kind="STREAM",
        stream_name="stdout",
        representations=(
            RuntimeOutputRepresentation(
                media_type="text/plain",
                encoding="UTF8",
                content=value,
            ),
        ),
    )


class RecordingJournalDriver:
    behavior: str

    def __init__(self, behavior: str = "success") -> None:
        self.behavior = behavior
        self.calls: list[str] = []
        self.records: list[RuntimeOutputRecord] = []
        self.offset = 0
        self.started = asyncio.Event()

    async def execute(
        self, _session_id: str, _code: str
    ) -> RuntimeExecutionResult:
        raise AssertionError("journal-capable driver used legacy execute")

    async def execute_streaming(
        self,
        _session_id: str,
        _code: str,
        output_handler: RuntimeOutputHandler,
    ) -> RuntimeExecutionResult:
        self.calls.append("execute")
        await output_handler(_record("first"))
        self.started.set()
        if self.behavior == "cancel":
            await asyncio.Event().wait()
        if self.behavior == "error":
            await output_handler(
                RuntimeOutputRecord(
                    kind="ERROR",
                    representations=(
                        RuntimeOutputRepresentation(
                            media_type="text/plain",
                            encoding="UTF8",
                            content="ValueError: expected",
                        ),
                    ),
                    metadata={
                        "ename": "ValueError",
                        "evalue": "expected",
                    },
                )
            )
            raise RuntimeExecutionError(
                "ValueError: expected",
                [
                    {
                        "output_type": "error",
                        "ename": "ValueError",
                        "evalue": "expected",
                        "traceback": [],
                    }
                ],
            )
        if self.behavior == "transport":
            raise RuntimeDriverError("Runtime connection failed.")
        await output_handler(_record("second"))
        return RuntimeExecutionResult(
            outputs=[
                {
                    "output_type": "stream",
                    "name": "stdout",
                    "text": "firstsecond",
                }
            ],
            execution_count=1,
        )

    async def output_journal_begin(
        self, _identity: RuntimeOutputJournalIdentity, source: str
    ) -> RuntimeOutputJournalDescriptor:
        assert source
        self.calls.append("begin")
        return self._descriptor("OPEN")

    async def output_journal_append(
        self,
        _identity: RuntimeOutputJournalIdentity,
        *,
        journal_id: UUID,
        expected_offset: int,
        batch_id: UUID,
        records: tuple[RuntimeOutputRecord, ...],
    ) -> RuntimeOutputAppendResult:
        assert journal_id == JOURNAL_ID
        assert expected_offset == self.offset
        assert len(records) == 1
        self.calls.append("append")
        self.records.extend(records)
        self.offset += 1
        output_id = uuid5(journal_id, f"output:{self.offset - 1}")
        representation = records[0].representations[0]
        return RuntimeOutputAppendResult(
            journal_id=journal_id,
            state="OPEN",
            batch_id=batch_id,
            committed_offset=self.offset,
            output_count=1,
            representation_count=1,
            total_bytes=len(records[0].representations[0].content),
            replayed=False,
            outputs=(
                RuntimeOutputDescriptor(
                    output_id=output_id,
                    ordinal=self.offset - 1,
                    kind=records[0].kind,
                    stream_name=records[0].stream_name,
                    execution_count=records[0].execution_count,
                    representations=(
                        RuntimeOutputRepresentationDescriptor(
                            representation_id=uuid5(
                                output_id, "representation:0"
                            ),
                            media_type=representation.media_type,
                            size_bytes=len(representation.content),
                            checksum_sha256="a" * 64,
                            complete=True,
                            content_ref=(
                                f"journal://{journal_id}/{output_id}/0"
                            ),
                        ),
                    ),
                    metadata=records[0].metadata,
                    created_at=datetime.now(UTC),
                ),
            ),
        )

    async def output_journal_finalize(
        self,
        _identity: RuntimeOutputJournalIdentity,
        *,
        journal_id: UUID,
    ) -> RuntimeOutputJournalDescriptor:
        assert journal_id == JOURNAL_ID
        self.calls.append("finalize")
        return self._descriptor("FINALIZED")

    async def output_journal_abort(
        self,
        _identity: RuntimeOutputJournalIdentity,
        *,
        journal_id: UUID,
        reason: str,
    ) -> RuntimeOutputJournalDescriptor:
        assert journal_id == JOURNAL_ID
        assert reason
        self.calls.append("abort")
        return self._descriptor("ABORTED")

    def _descriptor(self, state: str) -> RuntimeOutputJournalDescriptor:
        return RuntimeOutputJournalDescriptor(
            journal_id=JOURNAL_ID,
            state=state,
            committed_offset=self.offset,
            output_count=self.offset,
            representation_count=self.offset,
            total_bytes=sum(
                len(record.representations[0].content)
                for record in self.records
            ),
            checksum_sha256="c" * 64 if state == "FINALIZED" else None,
        )


class RecordingNotebookMaterializer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple[RuntimeNotebookCell, ...]]] = []

    async def materialize_notebook(
        self,
        workspace_path: str,
        runtime_profile: str,
        cells: tuple[RuntimeNotebookCell, ...],
    ) -> RuntimeNotebookMaterializationResult:
        self.calls.append((workspace_path, runtime_profile, cells))
        return RuntimeNotebookMaterializationResult(
            notebook_path=f"{workspace_path}/notebooks/execution.ipynb",
            cell_count=len(cells),
            output_count=2,
        )

    async def write_notebook(self, *_args: object) -> None:
        raise AssertionError("journal materialization must avoid Contents API")


class RecordingNotebookPreparer:
    def __init__(self) -> None:
        self.calls: list[
            tuple[str, UUID, str, tuple[RuntimeNotebookSourceCell, ...]]
        ] = []

    async def prepare_notebook(
        self,
        workspace_path: str,
        execution_id: UUID,
        runtime_profile: str,
        cells: tuple[RuntimeNotebookSourceCell, ...],
    ) -> RuntimeNotebookPreparationResult:
        self.calls.append(
            (workspace_path, execution_id, runtime_profile, cells)
        )
        return RuntimeNotebookPreparationResult(
            notebook_path=f"{workspace_path}/notebooks/execution.ipynb",
            prepared_cell_count=len(cells),
            total_cell_count=len(cells),
        )


def _worker() -> ExecutionWorker:
    return ExecutionWorker.__new__(ExecutionWorker)


async def test_worker_prefers_runtime_journal_notebook_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker()
    driver = RecordingNotebookMaterializer()
    identity = _identity()
    cell = RuntimeNotebookCell(
        sequence=0,
        execution_count=1,
        journal_id=JOURNAL_ID,
        journal=identity,
    )

    async def assert_active_lease(
        _worker: ExecutionWorker, _lease: object
    ) -> None:
        return None

    async def journal_notebook_cells(
        _worker: ExecutionWorker, _lease: object
    ) -> tuple[RuntimeNotebookCell, ...]:
        return (cell,)

    monkeypatch.setattr(
        ExecutionWorker, "_assert_active_lease", assert_active_lease
    )
    monkeypatch.setattr(
        ExecutionWorker, "_journal_notebook_cells", journal_notebook_cells
    )
    workspace_path = identity.workspace_path
    workspace = SimpleNamespace(
        runtime_relative_path=workspace_path,
        notebook_path=f"{workspace_path}/notebooks/execution.ipynb",
    )

    await worker._write_execution_notebook(
        cast(Any, driver),
        cast(Any, object()),
        "basic",
        cast(Any, workspace),
        ["print('complete')"],
        [[{"output_type": "stream", "text": "complete"}]],
        [1],
    )

    assert driver.calls == [(workspace_path, "basic", (cell,))]


async def test_worker_prepares_notebook_cells_before_runtime_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker()
    driver = RecordingNotebookPreparer()
    identity = _identity()

    async def assert_active_lease(
        _worker: ExecutionWorker, _lease: object
    ) -> None:
        return None

    async def trace_runtime(
        _worker: ExecutionWorker,
        _name: str,
        operation: Any,
        **_attributes: object,
    ) -> Any:
        return await operation

    monkeypatch.setattr(
        ExecutionWorker, "_assert_active_lease", assert_active_lease
    )
    monkeypatch.setattr(ExecutionWorker, "_trace_runtime", trace_runtime)
    workspace = SimpleNamespace(
        runtime_relative_path=identity.workspace_path,
        notebook_path=(f"{identity.workspace_path}/notebooks/execution.ipynb"),
    )
    step = SimpleNamespace(
        sequence=0,
        operation_id=identity.operation_id,
        id=identity.step_id,
        code="print('prepared')",
    )

    await worker._prepare_execution_notebook(
        cast(Any, driver),
        cast(Any, object()),
        identity.execution_id,
        "basic",
        cast(Any, workspace),
        cast(Any, [step]),
        identity.runtime_target_id,
    )

    assert len(driver.calls) == 1
    assert driver.calls[0][:3] == (
        identity.workspace_path,
        identity.execution_id,
        "basic",
    )
    assert driver.calls[0][3][0].source == "print('prepared')"


async def test_worker_journals_each_output_and_finalizes_success() -> None:
    driver = RecordingJournalDriver()

    result = await _worker()._execute_with_output_journal(
        cast(Any, driver), "kernel-1", "print('ok')", _identity()
    )

    assert result.execution_count == 1
    assert driver.calls == [
        "begin",
        "execute",
        "append",
        "append",
        "finalize",
    ]
    assert [
        record.representations[0].content for record in driver.records
    ] == [
        "first",
        "second",
    ]


async def test_worker_finalizes_complete_runtime_error_output() -> None:
    driver = RecordingJournalDriver("error")

    with pytest.raises(RuntimeExecutionError, match="expected"):
        await _worker()._execute_with_output_journal(
            cast(Any, driver), "kernel-1", "raise ValueError", _identity()
        )

    assert driver.calls == [
        "begin",
        "execute",
        "append",
        "append",
        "finalize",
    ]


async def test_worker_aborts_incomplete_transport_failure() -> None:
    driver = RecordingJournalDriver("transport")

    with pytest.raises(RuntimeDriverError, match="connection failed"):
        await _worker()._execute_with_output_journal(
            cast(Any, driver), "kernel-1", "work()", _identity()
        )

    assert driver.calls == ["begin", "execute", "append", "abort"]


async def test_worker_aborts_journal_when_execution_is_cancelled() -> None:
    driver = RecordingJournalDriver("cancel")
    task = asyncio.create_task(
        _worker()._execute_with_output_journal(
            cast(Any, driver), "kernel-1", "work()", _identity()
        )
    )
    await driver.started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert driver.calls == ["begin", "execute", "append", "abort"]


class LegacyDriver:
    called: ClassVar[bool] = False

    async def execute(
        self, _session_id: str, _code: str
    ) -> RuntimeExecutionResult:
        type(self).called = True
        return RuntimeExecutionResult(outputs=[], execution_count=1)


async def test_worker_keeps_legacy_driver_execution_compatible() -> None:
    LegacyDriver.called = False

    result = await _worker()._execute_with_output_journal(
        cast(Any, LegacyDriver()), "runtime", "code", _identity()
    )

    assert result.execution_count == 1
    assert LegacyDriver.called
