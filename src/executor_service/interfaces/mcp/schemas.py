"""MCP-only request wrappers.

Successful response contracts live in :mod:`executor_service.interfaces.contracts` and are
shared with REST response models.
"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from executor_service.execution_specs import ExecutionSpec, ExecutionStepInput
from executor_service.interfaces.contracts import (
    ActorInput,
    ExecutionArtifactMaterializeRequest,
    ExecutionSubmitRequest,
    RuntimeTargetUpsertRequest,
)

__all__ = [
    "ActorInput",
    "ExecutionArtifactMaterializeToolRequest",
    "ExecutionCancelRequest",
    "ExecutionFinalizeRequest",
    "ExecutionOperationCreateRequest",
    "ExecutionRetryRequest",
    "ExecutionSpec",
    "ExecutionStepInput",
    "ExecutionSubmitRequest",
    "RuntimeTargetDisableRequest",
    "RuntimeTargetProbeRequest",
    "RuntimeTargetSetStateRequest",
    "RuntimeTargetUpsertRequest",
]


class MCPModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExecutionCancelRequest(MCPModel):
    execution_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=255)
    reason: str | None = Field(default=None, max_length=2000)
    actor: ActorInput


class ExecutionRetryRequest(MCPModel):
    execution_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=255)
    actor: ActorInput


class ExecutionOperationCreateRequest(MCPModel):
    execution_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=255)
    expected_version: int = Field(ge=0)
    operation_timeout_seconds: int | None = Field(default=None, ge=1)
    spec: ExecutionSpec
    metadata: dict[str, object] = Field(default_factory=dict)
    actor: ActorInput


class ExecutionFinalizeRequest(MCPModel):
    execution_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=255)
    expected_version: int = Field(ge=0)
    actor: ActorInput


class ExecutionArtifactMaterializeToolRequest(
    ExecutionArtifactMaterializeRequest
):
    execution_id: UUID


class RuntimeTargetProbeRequest(MCPModel):
    target_id: UUID
    actor: ActorInput


class RuntimeTargetDisableRequest(MCPModel):
    idempotency_key: str = Field(min_length=1, max_length=255)
    target_id: UUID
    actor: ActorInput


class RuntimeTargetSetStateRequest(MCPModel):
    idempotency_key: str = Field(min_length=1, max_length=255)
    target_id: UUID
    desired_state: str = Field(pattern=r"^(ACTIVE|DRAINING)$")
    actor: ActorInput
