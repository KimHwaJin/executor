"""REST-only request and error contracts.

Successful response contracts live in :mod:`executor_service.interfaces.contracts` and are
shared with MCP Tool structured content.
"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from executor_service.application.runtime_targets import (
    DisableRuntimeTargetCommand,
    PurgeRuntimeTargetCommand,
    RuntimeTargetPurgeView,
    SetRuntimeTargetStateCommand,
)
from executor_service.domain.enums import RuntimeTargetStatus
from executor_service.execution_specs import CodeSource
from executor_service.interfaces.contracts import (
    ActorInput,
    AuditFields,
    ExecutionSubmitRequest,
    RuntimeTargetUpsertRequest,
)

__all__ = [
    "ActorInput",
    "ErrorResponse",
    "ExecutionCancelRequest",
    "ExecutionFinalizeRequest",
    "ExecutionOperationCreateRequest",
    "ExecutionRetryRequest",
    "ExecutionSubmitRequest",
    "RuntimeTargetMutationRequest",
    "RuntimeTargetProbeRequest",
    "RuntimeTargetPurgeRequest",
    "RuntimeTargetPurgeResponse",
    "RuntimeTargetUpsertRequest",
]


class HTTPModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RuntimeTargetProbeRequest(HTTPModel):
    actor: ActorInput


class RuntimeTargetMutationRequest(HTTPModel):
    idempotency_key: str = Field(min_length=1, max_length=255)
    actor: ActorInput

    def to_disable_command(self, target_id: UUID) -> DisableRuntimeTargetCommand:
        return DisableRuntimeTargetCommand(
            idempotency_key=self.idempotency_key,
            target_id=target_id,
            actor_type=self.actor.type,
            actor_id=self.actor.id,
        )

    def to_state_command(
        self, target_id: UUID, desired_state: RuntimeTargetStatus
    ) -> SetRuntimeTargetStateCommand:
        return SetRuntimeTargetStateCommand(
            idempotency_key=self.idempotency_key,
            target_id=target_id,
            desired_state=desired_state,
            actor_type=self.actor.type,
            actor_id=self.actor.id,
        )


class RuntimeTargetPurgeRequest(RuntimeTargetMutationRequest):
    confirmation_name: str = Field(min_length=1, max_length=255)

    def to_command(self, target_id: UUID) -> PurgeRuntimeTargetCommand:
        return PurgeRuntimeTargetCommand(
            idempotency_key=self.idempotency_key,
            target_id=target_id,
            confirmation_name=self.confirmation_name,
            actor_type=self.actor.type,
            actor_id=self.actor.id,
        )


class RuntimeTargetPurgeResponse(AuditFields):
    target_id: UUID
    name: str

    @classmethod
    def from_view(cls, view: RuntimeTargetPurgeView) -> "RuntimeTargetPurgeResponse":
        return cls(
            target_id=view.target_id,
            name=view.name,
            created_by_type=view.created_by_type,
            created_by=view.created_by,
            updated_by_type=view.updated_by_type,
            updated_by=view.updated_by,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )


class ExecutionCancelRequest(HTTPModel):
    idempotency_key: str = Field(min_length=1, max_length=255)
    reason: str | None = Field(default=None, max_length=2000)
    actor: ActorInput


class ExecutionRetryRequest(HTTPModel):
    idempotency_key: str = Field(min_length=1, max_length=255)
    actor: ActorInput


class ExecutionOperationCreateRequest(HTTPModel):
    idempotency_key: str = Field(min_length=1, max_length=255)
    expected_version: int = Field(ge=0)
    operation_timeout_seconds: int | None = Field(default=None, ge=1)
    source: CodeSource
    metadata: dict[str, object] = Field(default_factory=dict)
    actor: ActorInput


class ExecutionFinalizeRequest(HTTPModel):
    idempotency_key: str = Field(min_length=1, max_length=255)
    expected_version: int = Field(ge=0)
    actor: ActorInput


class ValidationIssue(HTTPModel):
    location: list[str | int]
    message: str
    type: str


class ErrorDetail(HTTPModel):
    code: str
    message: str
    details: list[ValidationIssue] | None = None


class ErrorResponse(HTTPModel):
    error: ErrorDetail
