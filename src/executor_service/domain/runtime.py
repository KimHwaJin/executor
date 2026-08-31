"""Runtime driver contracts shared by the application and infrastructure layers."""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable
from uuid import UUID

from executor_service.domain.enums import RuntimeAbortStatus, RuntimeType


class RuntimeDriverError(RuntimeError):
    """An execution runtime could not be reached or used."""


class ExecutionCompletionError(RuntimeError):
    """Required post-code delivery failed; replaying code is not a repair."""

    def __init__(self, phase: str) -> None:
        self.phase = phase
        super().__init__(
            f"Required execution completion failed at {phase}; "
            "successful Step results are preserved. Code retry is disabled. "
            "Inspect execution diagnostics for the underlying cause."
        )


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


RuntimeOutputLimitKind = Literal["MESSAGE_SIZE", "DATA_RATE", "MESSAGE_RATE"]


class RuntimeOutputLimitExceededError(RuntimeExecutionError):
    """A receive ceiling or Runtime rate limiter prevented full delivery."""

    def __init__(
        self,
        max_message_bytes: int | None = None,
        *,
        kind: RuntimeOutputLimitKind = "MESSAGE_SIZE",
        outputs: list[dict[str, Any]] | None = None,
    ) -> None:
        if kind == "MESSAGE_SIZE":
            if max_message_bytes is None or max_message_bytes <= 0:
                raise ValueError("A positive message size limit is required.")
            message = (
                "Runtime output message exceeded the configured "
                f"{max_message_bytes}-byte safety limit; the Step result is "
                "incomplete."
            )
        elif kind in {"DATA_RATE", "MESSAGE_RATE"}:
            if max_message_bytes is not None:
                raise ValueError("Rate limits do not specify a message size.")
            message = (
                f"Runtime output {kind.lower().replace('_', ' ')} limit "
                "was exceeded; the Runtime suppressed output delivery. "
                "The Step result is incomplete; reduce displayed output "
                "or write large results to artifacts."
            )
        else:
            raise ValueError("Unsupported Runtime output limit kind.")
        super().__init__(
            message, outputs=outputs if outputs is not None else []
        )
        self.max_message_bytes = max_message_bytes
        self.kind: RuntimeOutputLimitKind = kind


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
class RuntimeNotebookSourceCell:
    sequence: int
    operation_id: UUID
    step_id: UUID
    source: str


@dataclass(frozen=True, slots=True)
class RuntimeNotebookPreparationResult:
    notebook_path: str
    prepared_cell_count: int
    total_cell_count: int


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
class RuntimeNotebookPreparer(Protocol):
    async def prepare_notebook(
        self,
        workspace_path: str,
        execution_id: UUID,
        runtime_profile: str,
        cells: tuple[RuntimeNotebookSourceCell, ...],
    ) -> RuntimeNotebookPreparationResult: ...


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


@runtime_checkable
class RuntimeFileStreamer(Protocol):
    def stream_file(
        self, path: str, start: int, end: int
    ) -> AsyncIterator[bytes]: ...


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


class RuntimeArtifactContentAccess(Protocol):
    def stream_file(
        self,
        runtime_type: RuntimeType,
        preferred_target_id: UUID | None,
        path: str,
        start: int,
        end: int,
    ) -> AsyncIterator[bytes]: ...
