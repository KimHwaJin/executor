"""Public MCP request and response contracts; never reuse ORM models here."""

from datetime import datetime
from typing import Any, Self
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr, model_validator

from executor_service.application.commands import StepSpec as ApplicationStepSpec
from executor_service.application.commands import SubmitExecutionCommand
from executor_service.application.execution_queries import (
    ExecutionAttemptView,
    ExecutionEventView,
    ExecutionStepAttemptView,
    ExecutionTraceView,
)
from executor_service.application.jupyter_servers import JupyterServerView
from executor_service.domain.enums import (
    AttemptStatus,
    CodeSourceType,
    ExecutionMode,
    ExecutionStatus,
    JupyterPool,
    OutboxStatus,
    StepStatus,
    TriggerType,
)
from executor_service.domain.models import Execution


class MCPModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CodeSource(MCPModel):
    type: CodeSourceType
    code: str | None = None
    path: str | None = None

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        if self.type == CodeSourceType.INLINE and (self.code is None or self.path is not None):
            raise ValueError("INLINE source requires code and forbids path.")
        if self.type == CodeSourceType.PATH and (self.path is None or self.code is not None):
            raise ValueError("PATH source requires path and forbids code.")
        return self


class ExecutionContext(MCPModel):
    requested_by_user_id: str = Field(min_length=1, max_length=255)
    project_id: str = Field(min_length=1, max_length=255)
    session_id: str = Field(min_length=1, max_length=255)
    execution_plan_id: str = Field(min_length=1, max_length=255)
    workflow_id: str | None = Field(default=None, max_length=255)
    correlation_id: str | None = Field(default=None, max_length=255)


class ExecutionStepInput(MCPModel):
    sequence: int = Field(ge=0)
    skill_name: str | None = Field(default=None, max_length=255)
    tool_name: str | None = Field(default=None, max_length=255)
    input_parameters: dict[str, Any] = Field(default_factory=dict)


class ExecutionSubmitRequest(MCPModel):
    idempotency_key: str = Field(min_length=1, max_length=255)
    mode: ExecutionMode
    trigger_type: TriggerType = TriggerType.INTERACTIVE
    jupyter_pool: JupyterPool = JupyterPool.INTERACTIVE
    kernel_name: str = Field(min_length=1, max_length=128)
    source: CodeSource
    context: ExecutionContext
    metadata: dict[str, Any] = Field(default_factory=dict)
    steps: list[ExecutionStepInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_steps(self) -> Self:
        sequences = [step.sequence for step in self.steps]
        if len(sequences) != len(set(sequences)):
            raise ValueError("Step sequence values must be unique.")
        if sequences != sorted(sequences):
            raise ValueError("Steps must be ordered by sequence.")
        return self

    def to_command(self) -> SubmitExecutionCommand:
        return SubmitExecutionCommand(
            idempotency_key=self.idempotency_key,
            mode=self.mode,
            trigger_type=self.trigger_type,
            jupyter_pool=self.jupyter_pool,
            kernel_name=self.kernel_name,
            code_source_type=self.source.type,
            code=self.source.code,
            code_path=self.source.path,
            requested_by_user_id=self.context.requested_by_user_id,
            project_id=self.context.project_id,
            session_id=self.context.session_id,
            execution_plan_id=self.context.execution_plan_id,
            workflow_id=self.context.workflow_id,
            correlation_id=self.context.correlation_id,
            metadata=self.metadata,
            steps=tuple(
                ApplicationStepSpec(
                    sequence=step.sequence,
                    skill_name=step.skill_name,
                    tool_name=step.tool_name,
                    input_parameters=step.input_parameters,
                )
                for step in self.steps
            ),
        )


class ExecutionCancelRequest(MCPModel):
    execution_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=255)
    reason: str | None = Field(default=None, max_length=2000)


class ExecutionRetryRequest(MCPModel):
    execution_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=255)


class JupyterServerUpsertRequest(MCPModel):
    idempotency_key: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=255, pattern=r"^[a-zA-Z0-9._-]+$")
    endpoint: AnyHttpUrl
    token: SecretStr | None = None
    pool: JupyterPool = JupyterPool.INTERACTIVE
    max_concurrent_executions: int | None = Field(default=None, ge=1, le=1000)


class JupyterServerRemoveRequest(MCPModel):
    idempotency_key: str = Field(min_length=1, max_length=255)
    server_id: UUID


class JupyterServerSetStateRequest(MCPModel):
    idempotency_key: str = Field(min_length=1, max_length=255)
    server_id: UUID
    desired_state: str = Field(pattern=r"^(ACTIVE|DRAINING)$")


class JupyterServerResponse(MCPModel):
    server_id: UUID
    name: str
    endpoint: str
    pool: JupyterPool
    status: str
    enabled: bool
    max_concurrent_executions: int
    supported_kernels: tuple[str, ...]
    active_execution_count: int
    active_kernel_count: int | None
    last_health_check_at: datetime | None
    last_health_error: str | None
    created_at: datetime
    updated_at: datetime
    accepting_new_executions: bool
    drain_complete: bool

    @classmethod
    def from_view(cls, view: JupyterServerView) -> "JupyterServerResponse":
        return cls(
            server_id=view.id,
            name=view.name,
            endpoint=view.endpoint,
            pool=view.pool,
            status=view.status.value,
            enabled=view.enabled,
            max_concurrent_executions=view.max_concurrent_executions,
            supported_kernels=view.supported_kernels,
            active_execution_count=view.active_execution_count,
            active_kernel_count=view.active_kernel_count,
            last_health_check_at=view.last_health_check_at,
            last_health_error=view.last_health_error,
            created_at=view.created_at,
            updated_at=view.updated_at,
            accepting_new_executions=view.accepting_new_executions,
            drain_complete=view.drain_complete,
        )


class ExecutionStepResponse(MCPModel):
    id: UUID
    sequence: int
    skill_name: str | None
    tool_name: str | None
    status: StepStatus
    outputs: list[dict[str, Any]]
    error_message: str | None
    started_at: datetime | None
    finished_at: datetime | None


class ExecutionResponse(MCPModel):
    execution_id: UUID
    status: ExecutionStatus
    mode: ExecutionMode
    trigger_type: TriggerType
    jupyter_pool: JupyterPool
    kernel_name: str
    context: ExecutionContext
    steps: list[ExecutionStepResponse]
    cancellation_reason: str | None
    jupyter_server_id: UUID | None
    kernel_id: str | None
    workspace_path: str | None
    notebook_path: str | None
    error_message: str | None
    version: int
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    retryable: bool
    retry_from_sequence: int | None
    retained_kernel_until: datetime | None
    retry_count: int

    @classmethod
    def from_domain(cls, execution: Execution) -> "ExecutionResponse":
        return cls(
            execution_id=execution.id,
            status=execution.status,
            mode=execution.mode,
            trigger_type=execution.trigger_type,
            jupyter_pool=execution.jupyter_pool,
            kernel_name=execution.kernel_name,
            context=ExecutionContext(
                requested_by_user_id=execution.requested_by_user_id,
                project_id=execution.project_id,
                session_id=execution.session_id,
                execution_plan_id=execution.execution_plan_id,
                workflow_id=execution.workflow_id,
                correlation_id=execution.correlation_id,
            ),
            steps=[
                ExecutionStepResponse(
                    id=step.id,
                    sequence=step.sequence,
                    skill_name=step.skill_name,
                    tool_name=step.tool_name,
                    status=step.status,
                    outputs=step.outputs,
                    error_message=step.error_message,
                    started_at=step.started_at,
                    finished_at=step.finished_at,
                )
                for step in execution.steps
            ],
            cancellation_reason=execution.cancellation_reason,
            jupyter_server_id=execution.jupyter_server_id,
            kernel_id=execution.kernel_id,
            workspace_path=execution.workspace_path,
            notebook_path=execution.notebook_path,
            error_message=execution.error_message,
            version=execution.version,
            created_at=execution.created_at,
            updated_at=execution.updated_at,
            started_at=execution.started_at,
            finished_at=execution.finished_at,
            retryable=execution.retryable,
            retry_from_sequence=execution.retry_from_sequence,
            retained_kernel_until=execution.retained_kernel_until,
            retry_count=execution.retry_count,
        )


class ExecutionStepAttemptResponse(MCPModel):
    step_attempt_id: UUID
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

    @classmethod
    def from_view(
        cls, view: ExecutionStepAttemptView
    ) -> "ExecutionStepAttemptResponse":
        return cls(
            step_attempt_id=view.id,
            execution_step_id=view.execution_step_id,
            sequence=view.sequence,
            skill_name=view.skill_name,
            tool_name=view.tool_name,
            input_parameters=view.input_parameters,
            status=view.status,
            outputs=view.outputs,
            error_message=view.error_message,
            started_at=view.started_at,
            finished_at=view.finished_at,
        )


class ExecutionAttemptResponse(MCPModel):
    attempt_id: UUID
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
    steps: list[ExecutionStepAttemptResponse]

    @classmethod
    def from_view(cls, view: ExecutionAttemptView) -> "ExecutionAttemptResponse":
        return cls(
            attempt_id=view.id,
            execution_id=view.execution_id,
            attempt_number=view.attempt_number,
            jupyter_server_id=view.jupyter_server_id,
            kernel_id=view.kernel_id,
            status=view.status,
            lease_owner=view.lease_owner,
            lease_expires_at=view.lease_expires_at,
            heartbeat_at=view.heartbeat_at,
            error_message=view.error_message,
            started_at=view.started_at,
            finished_at=view.finished_at,
            steps=[ExecutionStepAttemptResponse.from_view(step) for step in view.steps],
        )


class ExecutionEventResponse(MCPModel):
    event_id: UUID
    event_type: str
    payload: dict[str, Any]
    delivery_status: OutboxStatus
    publish_attempt_count: int
    available_at: datetime
    created_at: datetime
    published_at: datetime | None
    last_error: str | None

    @classmethod
    def from_view(cls, view: ExecutionEventView) -> "ExecutionEventResponse":
        return cls(
            event_id=view.id,
            event_type=view.event_type,
            payload=view.payload,
            delivery_status=view.delivery_status,
            publish_attempt_count=view.publish_attempt_count,
            available_at=view.available_at,
            created_at=view.created_at,
            published_at=view.published_at,
            last_error=view.last_error,
        )


class ExecutionTraceResponse(MCPModel):
    execution: ExecutionResponse
    attempts: list[ExecutionAttemptResponse]
    events: list[ExecutionEventResponse]

    @classmethod
    def from_view(cls, view: ExecutionTraceView) -> "ExecutionTraceResponse":
        return cls(
            execution=ExecutionResponse.from_domain(view.execution),
            attempts=[ExecutionAttemptResponse.from_view(item) for item in view.attempts],
            events=[ExecutionEventResponse.from_view(item) for item in view.events],
        )


class ExecutorCapabilities(MCPModel):
    service: str = "executor-service"
    protocol_revision: str = "2026-07-28"
    mcp_tasks_supported: bool = False
    execution_modes: tuple[ExecutionMode, ...] = (
        ExecutionMode.STATIC,
        ExecutionMode.DYNAMIC,
    )
    code_source_types: tuple[CodeSourceType, ...] = (
        CodeSourceType.INLINE,
        CodeSourceType.PATH,
    )
    jupyter_pools: tuple[JupyterPool, ...] = (
        JupyterPool.INTERACTIVE,
        JupyterPool.BATCH,
    )
    event_delivery: str = "redis-streams-via-transactional-outbox"
    jupyter_execution_implemented: bool = True
    implemented_execution_modes: tuple[ExecutionMode, ...] = (ExecutionMode.STATIC,)
    tools: tuple[str, ...] = (
        "executor_get_capabilities",
        "execution_submit",
        "execution_get",
        "execution_cancel",
        "execution_retry",
    )
