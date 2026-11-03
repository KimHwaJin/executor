"""REST-only request and error contracts.

Successful response contracts live in :mod:`executor_service.interfaces.contracts` and are
shared with MCP Tool structured content.
"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from executor_service.application.maintenance_runs import (
    CreateMaintenanceRunCommand,
)
from executor_service.application.runtime_targets import (
    DisableRuntimeTargetCommand,
    RuntimeTargetPurgeView,
    SetRuntimeTargetStateCommand,
)
from executor_service.domain.enums import (
    MaintenanceRunAction,
    RuntimeTargetStatus,
)
from executor_service.interfaces.auth import Caller, require_user
from executor_service.interfaces.bounded_mappings import CallerMetadata
from executor_service.interfaces.contracts import (
    AuditFields,
    ExecutionSubmitRequest,
    RuntimeTargetUpsertRequest,
)
from executor_service.interfaces.execution_specs import ExecutionSpec

__all__ = [
    "ErrorResponse",
    "ExecutionCancelRequest",
    "ExecutionFinalizeRequest",
    "ExecutionOperationCreateRequest",
    "ExecutionRetryRequest",
    "ExecutionSubmitRequest",
    "ExecutorMaintenanceMutationRequest",
    "MaintenanceRunCreateRequest",
    "RuntimeTargetMutationRequest",
    "RuntimeTargetPurgeResponse",
    "RuntimeTargetUpsertRequest",
]


class HTTPModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExecutorMaintenanceMutationRequest(HTTPModel):
    idempotency_key: str = Field(min_length=1, max_length=255)


class MaintenanceRunCreateRequest(HTTPModel):
    idempotency_key: str = Field(min_length=1, max_length=255)
    action: MaintenanceRunAction

    def to_command(self, caller: Caller) -> CreateMaintenanceRunCommand:
        return CreateMaintenanceRunCommand(
            idempotency_key=self.idempotency_key,
            action=self.action,
            actor_client=caller.client,
            actor_id=require_user(caller),
        )


class RuntimeTargetMutationRequest(HTTPModel):
    idempotency_key: str = Field(min_length=1, max_length=255)

    def to_disable_command(
        self, target_id: UUID, caller: Caller
    ) -> DisableRuntimeTargetCommand:
        return DisableRuntimeTargetCommand(
            idempotency_key=self.idempotency_key,
            target_id=target_id,
            actor_client=caller.client,
            actor_id=require_user(caller),
        )

    def to_state_command(
        self,
        target_id: UUID,
        desired_state: RuntimeTargetStatus,
        caller: Caller,
    ) -> SetRuntimeTargetStateCommand:
        return SetRuntimeTargetStateCommand(
            idempotency_key=self.idempotency_key,
            target_id=target_id,
            desired_state=desired_state,
            actor_client=caller.client,
            actor_id=require_user(caller),
        )


class RuntimeTargetPurgeResponse(AuditFields):
    target_id: UUID
    name: str

    @classmethod
    def from_view(
        cls, view: RuntimeTargetPurgeView
    ) -> "RuntimeTargetPurgeResponse":
        return cls(
            target_id=view.target_id,
            name=view.name,
            created_by_client=view.created_by_client,
            created_by=view.created_by,
            updated_by_client=view.updated_by_client,
            updated_by=view.updated_by,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )


class ExecutionCancelRequest(HTTPModel):
    idempotency_key: str = Field(min_length=1, max_length=255)
    reason: str | None = Field(default=None, max_length=2000)


class ExecutionRetryRequest(HTTPModel):
    idempotency_key: str = Field(min_length=1, max_length=255)


class ExecutionOperationCreateRequest(HTTPModel):
    idempotency_key: str = Field(min_length=1, max_length=255)
    expected_version: int = Field(ge=0)
    operation_timeout_seconds: int | None = Field(default=None, ge=1)
    spec: ExecutionSpec
    metadata: CallerMetadata = Field(default_factory=dict)


class ExecutionFinalizeRequest(HTTPModel):
    idempotency_key: str = Field(min_length=1, max_length=255)
    expected_version: int = Field(ge=0)


class ValidationIssue(HTTPModel):
    location: list[str | int] = Field(
        description="Where the value came from (body or header), then its field path."
    )
    message: str
    type: str = Field(description="Stable snake_case reason.")
    context: dict[str, str | int | float | bool | None] | None = Field(
        default=None,
        description="The values behind the reason, such as limit and actual.",
    )


class ErrorDetail(HTTPModel):
    code: str
    message: str
    details: list[ValidationIssue] | None = None


class ErrorResponse(HTTPModel):
    error: ErrorDetail
