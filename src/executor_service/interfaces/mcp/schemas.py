"""MCP-only request wrappers.

Successful response contracts live in :mod:`executor_service.interfaces.contracts` and are
shared with REST response models.
"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from executor_service.interfaces.bounded_mappings import CallerMetadata
from executor_service.interfaces.contracts import (
    ExecutionArtifactMaterializeRequest,
    ExecutionSubmitRequest,
    RuntimeTargetUpsertRequest,
)
from executor_service.interfaces.execution_specs import (
    ExecutionSpec,
    ExecutionStepInput,
)

__all__ = [
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


class ExecutionRetryRequest(MCPModel):
    execution_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=255)


class ExecutionOperationCreateRequest(MCPModel):
    execution_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=255)
    expected_version: int = Field(ge=0)
    operation_timeout_seconds: int | None = Field(default=None, ge=1)
    spec: ExecutionSpec
    metadata: CallerMetadata = Field(default_factory=dict)


class ExecutionFinalizeRequest(MCPModel):
    execution_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=255)
    expected_version: int = Field(ge=0)


class ExecutionArtifactMaterializeToolRequest(
    ExecutionArtifactMaterializeRequest
):
    execution_id: UUID


class RuntimeTargetProbeRequest(MCPModel):
    target_id: UUID


class RuntimeTargetDisableRequest(MCPModel):
    idempotency_key: str = Field(min_length=1, max_length=255)
    target_id: UUID


class RuntimeTargetSetStateRequest(MCPModel):
    idempotency_key: str = Field(min_length=1, max_length=255)
    target_id: UUID
    desired_state: str = Field(pattern=r"^(ACTIVE|DRAINING)$")
