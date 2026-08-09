"""Application-facing read models for complete execution tracing."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from executor_service.domain.enums import AttemptStatus, OutboxStatus, StepStatus
from executor_service.domain.models import Execution


@dataclass(frozen=True, slots=True)
class ExecutionStepAttemptView:
    id: UUID
    execution_attempt_id: UUID
    execution_step_id: UUID
    sequence: int
    skill_name: str | None
    tool_name: str | None
    input_parameters: dict[str, Any]
    status: StepStatus
    outputs: list[dict[str, Any]]
    error_message: str | None
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class ExecutionAttemptView:
    id: UUID
    execution_id: UUID
    attempt_number: int
    jupyter_server_id: UUID
    kernel_id: str | None
    status: AttemptStatus
    lease_owner: str
    lease_expires_at: datetime
    heartbeat_at: datetime
    error_message: str | None
    started_at: datetime
    finished_at: datetime | None
    steps: tuple[ExecutionStepAttemptView, ...]


@dataclass(frozen=True, slots=True)
class ExecutionEventView:
    id: UUID
    event_type: str
    payload: dict[str, Any]
    delivery_status: OutboxStatus
    publish_attempt_count: int
    available_at: datetime
    created_at: datetime
    published_at: datetime | None
    last_error: str | None


@dataclass(frozen=True, slots=True)
class ExecutionTraceView:
    execution: Execution
    attempts: tuple[ExecutionAttemptView, ...]
    events: tuple[ExecutionEventView, ...]


class ExecutionQueryService(Protocol):
    async def attempts(
        self, execution_id: UUID, *, limit: int = 100
    ) -> list[ExecutionAttemptView]: ...

    async def events(
        self, execution_id: UUID, *, limit: int = 200
    ) -> list[ExecutionEventView]: ...

    async def trace(self, execution_id: UUID) -> ExecutionTraceView: ...
