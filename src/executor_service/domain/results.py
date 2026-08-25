"""Runtime-neutral contracts for immutable shared-volume execution results."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from executor_service.domain.runtime import RuntimeOutputRecord

RESULT_MANIFEST_SCHEMA_VERSION = "1.0"


@dataclass(frozen=True, slots=True)
class ExecutionSourceReference:
    relative_path: str
    checksum_sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class StepResultIdentity:
    execution_id: UUID
    operation_id: UUID
    step_id: UUID
    sequence: int
    execution_attempt_id: UUID
    fencing_token: int


@dataclass(frozen=True, slots=True)
class StepResultReference:
    relative_path: str
    checksum_sha256: str
    execution_attempt_id: UUID
    fencing_token: int


@dataclass(frozen=True, slots=True)
class StepResultDescriptor:
    state: str
    complete: bool
    reference: StepResultReference
    source: ExecutionSourceReference
    output_count: int
    representation_count: int
    total_size_bytes: int
    output_summary: dict[str, object]
    execution_count: int | None
    error_message: str | None


@dataclass(frozen=True, slots=True)
class StepResultProjection:
    outputs: list[dict[str, object]]
    execution_count: int | None


@dataclass(frozen=True, slots=True)
class StepResultAppend:
    committed_offset: int
    output_count: int
    representation_count: int
    total_size_bytes: int
    replayed: bool


class ExecutionResultStore(Protocol):
    async def snapshot_source(
        self,
        execution_id: UUID,
        step_id: UUID,
        content: str,
    ) -> ExecutionSourceReference: ...

    async def read_source(
        self, reference: ExecutionSourceReference
    ) -> str: ...

    async def read_step_outputs(
        self, reference: StepResultReference
    ) -> list[dict[str, object]]: ...

    async def read_step_projection(
        self, reference: StepResultReference
    ) -> StepResultProjection: ...

    async def begin_step_result(
        self,
        identity: StepResultIdentity,
        source: ExecutionSourceReference,
    ) -> None: ...

    async def append_step_outputs(
        self,
        identity: StepResultIdentity,
        *,
        expected_offset: int,
        batch_id: UUID,
        records: tuple[RuntimeOutputRecord, ...],
    ) -> StepResultAppend: ...

    async def finalize_step_result(
        self,
        identity: StepResultIdentity,
        *,
        execution_count: int | None,
        error_message: str | None = None,
    ) -> StepResultDescriptor: ...

    async def abort_step_result(
        self,
        identity: StepResultIdentity,
        *,
        reason: str,
    ) -> StepResultDescriptor: ...
