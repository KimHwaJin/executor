"""Public MCP request and response contracts; never reuse ORM models here."""

from datetime import datetime
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr, model_validator

from executor_service.application.commands import StepSpec as ApplicationStepSpec
from executor_service.application.commands import SubmitExecutionCommand
from executor_service.application.execution_queries import (
    ExecutionArtifactView,
    ExecutionAttemptView,
    ExecutionEventView,
    ExecutionStepAttemptView,
    ExecutionTraceView,
)
from executor_service.application.jupyter_servers import JupyterServerView
from executor_service.domain.enums import (
    ArtifactStatus,
    ArtifactStorageType,
    ArtifactType,
    AttemptStatus,
    CodeSourceType,
    ExecutionMode,
    ExecutionStatus,
    FailureType,
    JupyterPool,
    KernelCleanupStatus,
    OutboxStatus,
    RetryStrategy,
    StepStatus,
    TriggerType,
)
from executor_service.domain.models import Execution


class MCPModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExecutionStepInput(MCPModel):
    sequence: int = Field(ge=0)
    plan_step_id: str = Field(min_length=1, max_length=255)
    skill_name: str | None = Field(default=None, max_length=255)
    tool_name: str | None = Field(default=None, max_length=255)
    input_parameters: dict[str, Any] = Field(default_factory=dict)
    code: str = Field(min_length=1)


class ExecutionSpec(MCPModel):
    schema_version: Literal["1.0"]
    execution_plan_id: str = Field(min_length=1, max_length=255)
    steps: list[ExecutionStepInput] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_steps(self) -> Self:
        sequences = [step.sequence for step in self.steps]
        expected = list(range(sequences[0], sequences[0] + len(sequences)))
        if sequences != expected:
            raise ValueError("Step sequence values must be contiguous and ordered.")
        plan_step_ids = [step.plan_step_id for step in self.steps]
        if len(plan_step_ids) != len(set(plan_step_ids)):
            raise ValueError("plan_step_id values must be unique within an ExecutionSpec.")
        if any(not step.code.strip() for step in self.steps):
            raise ValueError("Step code must not be blank.")
        return self


class InlineCodeSource(MCPModel):
    type: Literal[CodeSourceType.INLINE]
    spec: ExecutionSpec


class PathCodeSource(MCPModel):
    type: Literal[CodeSourceType.PATH]
    path: str = Field(min_length=1, max_length=4096)
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")


CodeSource = Annotated[InlineCodeSource | PathCodeSource, Field(discriminator="type")]


class ExecutionSubmitContext(MCPModel):
    requested_by_user_id: str = Field(min_length=1, max_length=255)
    project_id: str = Field(min_length=1, max_length=255)
    session_id: str = Field(min_length=1, max_length=255)
    task_id: str = Field(min_length=1, max_length=255)
    workflow_id: str | None = Field(default=None, max_length=255)


class ExecutionContext(ExecutionSubmitContext):
    execution_plan_id: str = Field(min_length=1, max_length=255)


class ExecutionSubmitRequest(MCPModel):
    idempotency_key: str = Field(min_length=1, max_length=255)
    mode: ExecutionMode
    trigger_type: TriggerType = TriggerType.INTERACTIVE
    kernel_name: str = Field(min_length=1, max_length=128)
    source: CodeSource
    context: ExecutionSubmitContext
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_command(
        self,
        spec: ExecutionSpec,
        *,
        source_content: str,
        source_sha256: str,
    ) -> SubmitExecutionCommand:
        return SubmitExecutionCommand(
            idempotency_key=self.idempotency_key,
            mode=self.mode,
            trigger_type=self.trigger_type,
            kernel_name=self.kernel_name,
            code_source_type=self.source.type,
            source_content=source_content,
            code_path=self.source.path if isinstance(self.source, PathCodeSource) else None,
            source_sha256=source_sha256,
            requested_by_user_id=self.context.requested_by_user_id,
            project_id=self.context.project_id,
            session_id=self.context.session_id,
            task_id=self.context.task_id,
            execution_plan_id=spec.execution_plan_id,
            workflow_id=self.context.workflow_id,
            metadata=self.metadata,
            steps=tuple(
                ApplicationStepSpec(
                    sequence=step.sequence,
                    code=step.code,
                    execution_plan_id=spec.execution_plan_id,
                    plan_step_id=step.plan_step_id,
                    skill_name=step.skill_name,
                    tool_name=step.tool_name,
                    input_parameters=step.input_parameters,
                )
                for step in spec.steps
            ),
        )


class ExecutionCancelRequest(MCPModel):
    execution_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=255)
    reason: str | None = Field(default=None, max_length=2000)


class ExecutionRetryRequest(MCPModel):
    execution_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=255)


class ExecutionContinueRequest(MCPModel):
    execution_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=255)
    expected_version: int = Field(ge=0)
    source: CodeSource


class ExecutionFinishRequest(MCPModel):
    execution_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=255)
    expected_version: int = Field(ge=0)


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
    code_hash: str | None
    execution_plan_id: str
    plan_step_id: str
    skill_name: str | None
    tool_name: str | None
    status: StepStatus
    outputs: list[dict[str, Any]]
    error_message: str | None
    started_at: datetime | None
    finished_at: datetime | None


class ExecutionSourceResponse(MCPModel):
    type: CodeSourceType
    path: str | None
    sha256: str


class ExecutionResponse(MCPModel):
    execution_id: UUID
    status: ExecutionStatus
    mode: ExecutionMode
    trigger_type: TriggerType
    jupyter_pool: JupyterPool
    kernel_name: str
    source: ExecutionSourceResponse
    context: ExecutionContext
    steps: list[ExecutionStepResponse]
    cancellation_reason: str | None
    jupyter_server_id: UUID | None
    kernel_id: str | None
    workspace_path: str | None
    notebook_path: str | None
    error_message: str | None
    failure_type: FailureType | None
    retry_strategy: RetryStrategy
    recovery_count: int
    kernel_cleanup_status: KernelCleanupStatus
    version: int
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    retryable: bool
    retry_from_sequence: int | None
    retained_kernel_until: datetime | None
    retry_count: int
    dynamic_wait_expires_at: datetime | None
    execution_expires_at: datetime | None

    @classmethod
    def from_domain(cls, execution: Execution) -> "ExecutionResponse":
        return cls(
            execution_id=execution.id,
            status=execution.status,
            mode=execution.mode,
            trigger_type=execution.trigger_type,
            jupyter_pool=execution.jupyter_pool,
            kernel_name=execution.kernel_name,
            source=ExecutionSourceResponse(
                type=execution.code_source_type,
                path=execution.code_path,
                sha256=execution.source_sha256,
            ),
            context=ExecutionContext(
                requested_by_user_id=execution.requested_by_user_id,
                project_id=execution.project_id,
                session_id=execution.session_id,
                task_id=execution.task_id,
                execution_plan_id=execution.execution_plan_id,
                workflow_id=execution.workflow_id,
            ),
            steps=[
                ExecutionStepResponse(
                    id=step.id,
                    sequence=step.sequence,
                    code_hash=step.code_hash,
                    execution_plan_id=step.execution_plan_id,
                    plan_step_id=step.plan_step_id,
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
            failure_type=execution.failure_type,
            retry_strategy=execution.retry_strategy,
            recovery_count=execution.recovery_count,
            kernel_cleanup_status=execution.kernel_cleanup_status,
            version=execution.version,
            created_at=execution.created_at,
            updated_at=execution.updated_at,
            started_at=execution.started_at,
            finished_at=execution.finished_at,
            retryable=execution.retryable,
            retry_from_sequence=execution.retry_from_sequence,
            retained_kernel_until=execution.retained_kernel_until,
            retry_count=execution.retry_count,
            dynamic_wait_expires_at=execution.dynamic_wait_expires_at,
            execution_expires_at=execution.execution_expires_at,
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
    lease_owner: str | None
    lease_expires_at: datetime | None
    heartbeat_at: datetime
    error_message: str | None
    failure_type: FailureType | None
    retry_strategy: RetryStrategy
    kernel_cleanup_status: KernelCleanupStatus
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
            failure_type=view.failure_type,
            retry_strategy=view.retry_strategy,
            kernel_cleanup_status=view.kernel_cleanup_status,
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


class ExecutionArtifactResponse(MCPModel):
    artifact_id: UUID
    execution_id: UUID
    execution_attempt_id: UUID
    execution_step_id: UUID | None
    execution_step_attempt_id: UUID | None
    parent_artifact_id: UUID | None
    external_parent_asset_id: str | None
    artifact_type: ArtifactType
    storage_type: ArtifactStorageType
    status: ArtifactStatus
    name: str
    description: str | None
    uri: str
    relative_path: str | None
    media_type: str | None
    size_bytes: int | None
    checksum_sha256: str | None
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_view(cls, view: ExecutionArtifactView) -> "ExecutionArtifactResponse":
        return cls(
            artifact_id=view.id,
            execution_id=view.execution_id,
            execution_attempt_id=view.execution_attempt_id,
            execution_step_id=view.execution_step_id,
            execution_step_attempt_id=view.execution_step_attempt_id,
            parent_artifact_id=view.parent_artifact_id,
            external_parent_asset_id=view.external_parent_asset_id,
            artifact_type=view.artifact_type,
            storage_type=view.storage_type,
            status=view.status,
            name=view.name,
            description=view.description,
            uri=view.uri,
            relative_path=view.relative_path,
            media_type=view.media_type,
            size_bytes=view.size_bytes,
            checksum_sha256=view.checksum_sha256,
            metadata=view.metadata,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )


class ExecutionTraceResponse(MCPModel):
    execution: ExecutionResponse
    attempts: list[ExecutionAttemptResponse]
    events: list[ExecutionEventResponse]
    artifacts: list[ExecutionArtifactResponse]

    @classmethod
    def from_view(cls, view: ExecutionTraceView) -> "ExecutionTraceResponse":
        return cls(
            execution=ExecutionResponse.from_domain(view.execution),
            attempts=[ExecutionAttemptResponse.from_view(item) for item in view.attempts],
            events=[ExecutionEventResponse.from_view(item) for item in view.events],
            artifacts=[ExecutionArtifactResponse.from_view(item) for item in view.artifacts],
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
    implemented_execution_modes: tuple[ExecutionMode, ...] = (
        ExecutionMode.STATIC,
        ExecutionMode.DYNAMIC,
    )
    failure_types: tuple[FailureType, ...] = tuple(FailureType)
    retry_strategies: tuple[RetryStrategy, ...] = tuple(RetryStrategy)
    tools: tuple[str, ...] = (
        "executor_get_capabilities",
        "execution_submit",
        "execution_get",
        "execution_cancel",
        "execution_retry",
        "execution_continue",
        "execution_finish",
    )
