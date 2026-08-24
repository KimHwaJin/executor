"""Runtime driver contracts shared by the application and infrastructure layers."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from executor_service.domain.enums import RuntimeAbortStatus, RuntimeType


class RuntimeDriverError(RuntimeError):
    """An execution runtime could not be reached or used."""


class RuntimeExecutionError(RuntimeDriverError):
    def __init__(self, message: str, outputs: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.outputs = outputs


class RuntimeExecutionTimeoutError(RuntimeExecutionError):
    def __init__(self, scope: str, timeout_seconds: float) -> None:
        super().__init__(
            f"{scope} timeout expired after {timeout_seconds:.3f} seconds.",
            outputs=[],
        )
        self.scope = scope
        self.timeout_seconds = timeout_seconds


@dataclass(frozen=True, slots=True)
class RuntimeExecutionResult:
    outputs: list[dict[str, Any]]
    execution_count: int | None


@dataclass(frozen=True, slots=True)
class RuntimeOutputRepresentation:
    media_type: str
    encoding: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeOutputRecord:
    kind: str
    representations: tuple[RuntimeOutputRepresentation, ...]
    stream_name: str | None = None
    execution_count: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeOutputJournalIdentity:
    workspace_path: str
    execution_id: UUID
    operation_id: UUID
    step_id: UUID
    sequence: int
    execution_attempt_id: UUID
    fencing_token: int
    runtime_target_id: UUID
    runtime_session_id: str


@dataclass(frozen=True, slots=True)
class RuntimeOutputJournalDescriptor:
    journal_id: UUID
    state: str
    committed_offset: int
    output_count: int
    representation_count: int
    total_bytes: int
    checksum_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeOutputRepresentationDescriptor:
    representation_id: UUID
    media_type: str
    size_bytes: int
    checksum_sha256: str
    complete: bool
    content_ref: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeOutputDescriptor:
    output_id: UUID
    ordinal: int
    kind: str
    stream_name: str | None
    execution_count: int | None
    representations: tuple[RuntimeOutputRepresentationDescriptor, ...]
    metadata: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RuntimeOutputAppendResult:
    journal_id: UUID
    state: str
    batch_id: UUID
    committed_offset: int
    output_count: int
    representation_count: int
    total_bytes: int
    replayed: bool
    outputs: tuple[RuntimeOutputDescriptor, ...]


@dataclass(frozen=True, slots=True)
class RuntimeNotebookCell:
    sequence: int
    execution_count: int | None
    journal_id: UUID
    journal: RuntimeOutputJournalIdentity


@dataclass(frozen=True, slots=True)
class RuntimeNotebookMaterializationResult:
    notebook_path: str
    cell_count: int
    output_count: int


RuntimeOutputHandler = Callable[[RuntimeOutputRecord], Awaitable[None]]


@runtime_checkable
class RuntimeStreamingExecutor(Protocol):
    async def execute_streaming(
        self,
        session_id: str,
        code: str,
        output_handler: RuntimeOutputHandler,
    ) -> RuntimeExecutionResult: ...


@runtime_checkable
class RuntimeOutputJournal(Protocol):
    async def output_journal_begin(
        self, identity: RuntimeOutputJournalIdentity, source: str
    ) -> RuntimeOutputJournalDescriptor: ...

    async def output_journal_append(
        self,
        identity: RuntimeOutputJournalIdentity,
        *,
        journal_id: UUID,
        expected_offset: int,
        batch_id: UUID,
        records: tuple[RuntimeOutputRecord, ...],
    ) -> RuntimeOutputAppendResult: ...

    async def output_journal_finalize(
        self,
        identity: RuntimeOutputJournalIdentity,
        *,
        journal_id: UUID,
    ) -> RuntimeOutputJournalDescriptor: ...

    async def output_journal_abort(
        self,
        identity: RuntimeOutputJournalIdentity,
        *,
        journal_id: UUID,
        reason: str,
    ) -> RuntimeOutputJournalDescriptor: ...


@runtime_checkable
class RuntimeNotebookMaterializer(Protocol):
    async def materialize_notebook(
        self,
        workspace_path: str,
        runtime_profile: str,
        cells: tuple[RuntimeNotebookCell, ...],
    ) -> RuntimeNotebookMaterializationResult: ...


@dataclass(frozen=True, slots=True)
class RuntimeAbortResult:
    status: RuntimeAbortStatus
    message: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeResourceMetric:
    used: float | int | None
    capacity: float | int | None
    utilization: float | None
    source: str | None
    estimated: bool | None
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RuntimeResourceObservation:
    observed_at: datetime
    process_count: int | None
    cpu: RuntimeResourceMetric
    memory: RuntimeResourceMetric


@dataclass(frozen=True, slots=True)
class RuntimeFileState:
    path: str
    size_bytes: int
    modified_ns: int


@dataclass(frozen=True, slots=True)
class RuntimeStorageSnapshot:
    files: tuple[RuntimeFileState, ...]
    manifest_size: int


@dataclass(frozen=True, slots=True)
class RuntimeFileMetadata:
    path: str
    name: str
    size_bytes: int
    modified_ns: int
    media_type: str | None
    checksum_sha256: str


class RuntimeStorage(Protocol):
    async def prepare_workspace(self, workspace_path: str) -> None: ...

    async def artifact_snapshot(
        self, workspace_path: str
    ) -> RuntimeStorageSnapshot: ...

    async def file_metadata(self, path: str) -> RuntimeFileMetadata: ...

    async def read_manifest(
        self, workspace_path: str, start: int
    ) -> bytes: ...

    async def write_notebook(
        self, path: str, notebook: dict[str, Any]
    ) -> None: ...

    async def read_notebook(self, path: str) -> dict[str, Any]: ...

    async def write_text(self, path: str, content: str) -> None: ...


class RuntimeDriver(RuntimeStorage, Protocol):
    async def close(self) -> None: ...

    async def status(self) -> dict[str, Any]: ...

    async def supported_profiles(self) -> list[str]: ...

    async def resource_status(self) -> RuntimeResourceObservation: ...

    async def start_session(
        self, profile: str, working_directory: str
    ) -> str: ...

    async def interrupt_session(self, session_id: str) -> None: ...

    async def abort_session(
        self, session_id: str, timeout_seconds: float
    ) -> RuntimeAbortResult: ...

    async def delete_session(self, session_id: str) -> None: ...

    async def session_exists(self, session_id: str) -> bool: ...

    async def execute(
        self, session_id: str, code: str
    ) -> RuntimeExecutionResult: ...


class RuntimeDriverFactory(Protocol):
    def create(
        self,
        runtime_type: RuntimeType,
        connection_config: dict[str, Any],
        credential: str,
    ) -> RuntimeDriver: ...


class RuntimeStorageAccess(Protocol):
    async def read_notebook(
        self,
        runtime_type: RuntimeType,
        preferred_target_id: UUID | None,
        path: str,
    ) -> dict[str, Any]: ...

    async def write_notebook(
        self,
        runtime_type: RuntimeType,
        preferred_target_id: UUID | None,
        path: str,
        notebook: dict[str, Any],
    ) -> None: ...

    async def write_text(
        self,
        runtime_type: RuntimeType,
        preferred_target_id: UUID | None,
        path: str,
        content: str,
    ) -> RuntimeFileMetadata: ...
