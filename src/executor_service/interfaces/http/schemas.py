"""REST request and response contracts, independent from MCP transport types."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from executor_service.application.commands import StepSpec, SubmitExecutionCommand
from executor_service.application.execution_queries import (
    ExecutionArtifactView,
    ExecutionAttemptView,
    ExecutionEventView,
    ExecutionStepAttemptView,
    ExecutionTraceView,
)
from executor_service.application.pagination import Page
from executor_service.application.runtime_targets import (
    PurgeRuntimeTargetCommand,
    RemoveRuntimeTargetCommand,
    RuntimePoolView,
    RuntimeTargetPurgeView,
    RuntimeTargetView,
    SetRuntimeTargetStateCommand,
    UpsertRuntimeTargetCommand,
)
from executor_service.domain.enums import (
    ActorType,
    ArtifactStatus,
    ArtifactStorageType,
    ArtifactType,
    AttemptStatus,
    CodeSourceType,
    ExecutionMode,
    ExecutionStatus,
    FailureType,
    OutboxStatus,
    RetryStrategy,
    RuntimePool,
    RuntimeSessionCleanupStatus,
    RuntimeTargetStatus,
    RuntimeType,
    StepStatus,
    TriggerType,
)
from executor_service.domain.models import Execution, ExecutionStep
from executor_service.execution_specs import CodeSource, ExecutionSpec, PathCodeSource


class HTTPModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ActorInput(HTTPModel):
    type: ActorType
    id: str = Field(min_length=1, max_length=255)


class RuntimeTargetUpsertRequest(HTTPModel):
    idempotency_key: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=255, pattern=r"^[a-zA-Z0-9._-]+$")
    runtime_type: RuntimeType
    connection_config: dict[str, Any]
    credential: SecretStr | None = None
    pool: RuntimePool
    max_concurrent_executions: int | None = Field(default=None, ge=1, le=1000)
    actor: ActorInput

    @model_validator(mode="after")
    def validate_connection_config(self) -> "RuntimeTargetUpsertRequest":
        if self.runtime_type == RuntimeType.JUPYTER:
            endpoint = self.connection_config.get("endpoint")
            if set(self.connection_config) != {"endpoint"} or not isinstance(endpoint, str):
                raise ValueError(
                    "JUPYTER connection_config must contain only a non-empty endpoint."
                )
            if not endpoint.startswith(("http://", "https://")):
                raise ValueError("JUPYTER endpoint must use http or https.")
        return self

    def to_command(self) -> UpsertRuntimeTargetCommand:
        return UpsertRuntimeTargetCommand(
            idempotency_key=self.idempotency_key,
            name=self.name,
            runtime_type=self.runtime_type,
            connection_config=self.connection_config,
            credential=(self.credential.get_secret_value() if self.credential else None),
            pool=self.pool,
            max_concurrent_executions=self.max_concurrent_executions,
            actor_type=self.actor.type,
            actor_id=self.actor.id,
        )


class RuntimeTargetProbeRequest(HTTPModel):
    actor: ActorInput


class RuntimeTargetMutationRequest(HTTPModel):
    idempotency_key: str = Field(min_length=1, max_length=255)
    actor: ActorInput

    def to_remove_command(self, target_id: UUID) -> RemoveRuntimeTargetCommand:
        return RemoveRuntimeTargetCommand(
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


class RuntimeTargetResponse(HTTPModel):
    target_id: UUID
    name: str
    runtime_type: RuntimeType
    pool: RuntimePool
    status: RuntimeTargetStatus
    enabled: bool
    accepting_new_executions: bool
    drain_complete: bool
    max_concurrent_executions: int
    active_execution_count: int
    available_capacity: int
    active_session_count: int | None
    supported_profiles: list[str]
    last_health_check_at: datetime | None
    last_health_error: str | None
    created_by_type: ActorType | None
    created_by: str | None
    updated_by_type: ActorType | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_view(cls, view: RuntimeTargetView) -> "RuntimeTargetResponse":
        return cls(
            target_id=view.id,
            name=view.name,
            runtime_type=view.runtime_type,
            pool=view.pool,
            status=view.status,
            enabled=view.enabled,
            accepting_new_executions=view.accepting_new_executions,
            drain_complete=view.drain_complete,
            max_concurrent_executions=view.max_concurrent_executions,
            active_execution_count=view.active_execution_count,
            available_capacity=view.available_capacity,
            active_session_count=view.active_session_count,
            supported_profiles=list(view.supported_profiles),
            last_health_check_at=view.last_health_check_at,
            last_health_error=view.last_health_error,
            created_by_type=view.created_by_type,
            created_by=view.created_by,
            updated_by_type=view.updated_by_type,
            updated_by=view.updated_by,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )


class RuntimeTargetPageResponse(HTTPModel):
    items: list[RuntimeTargetResponse]
    next_cursor: str | None
    has_more: bool

    @classmethod
    def from_page(cls, page: Page[RuntimeTargetView]) -> "RuntimeTargetPageResponse":
        return cls(
            items=[RuntimeTargetResponse.from_view(item) for item in page.items],
            next_cursor=page.next_cursor,
            has_more=page.next_cursor is not None,
        )


class RuntimePoolResponse(HTTPModel):
    runtime_type: RuntimeType
    pool: RuntimePool
    target_count: int
    enabled_target_count: int
    active_target_count: int
    draining_target_count: int
    offline_target_count: int
    configured_capacity: int
    schedulable_capacity: int
    active_execution_count: int
    available_capacity: int
    accepting_new_executions: bool
    saturated: bool
    last_health_check_at: datetime | None

    @classmethod
    def from_view(cls, view: RuntimePoolView) -> "RuntimePoolResponse":
        return cls(
            runtime_type=view.runtime_type,
            pool=view.pool,
            target_count=view.target_count,
            enabled_target_count=view.enabled_target_count,
            active_target_count=view.active_target_count,
            draining_target_count=view.draining_target_count,
            offline_target_count=view.offline_target_count,
            configured_capacity=view.configured_capacity,
            schedulable_capacity=view.schedulable_capacity,
            active_execution_count=view.active_execution_count,
            available_capacity=view.available_capacity,
            accepting_new_executions=view.accepting_new_executions,
            saturated=view.saturated,
            last_health_check_at=view.last_health_check_at,
        )


class RuntimePoolPageResponse(HTTPModel):
    items: list[RuntimePoolResponse]


class RuntimeTargetPurgeResponse(HTTPModel):
    target_id: UUID
    name: str
    runtime_type: RuntimeType
    pool: RuntimePool
    purged_by_type: ActorType | None
    purged_by: str | None
    purged_at: datetime

    @classmethod
    def from_view(cls, view: RuntimeTargetPurgeView) -> "RuntimeTargetPurgeResponse":
        return cls(
            target_id=view.target_id,
            name=view.name,
            runtime_type=view.runtime_type,
            pool=view.pool,
            purged_by_type=view.purged_by_type,
            purged_by=view.purged_by,
            purged_at=view.purged_at,
        )


class ExecutionSubmitContext(HTTPModel):
    user_id: str = Field(min_length=1, max_length=255)
    project_id: str = Field(min_length=1, max_length=255)
    session_id: str = Field(min_length=1, max_length=255)
    task_id: str = Field(min_length=1, max_length=255)
    workflow_id: str | None = Field(default=None, max_length=255)


class ExecutionContext(ExecutionSubmitContext):
    execution_plan_id: str = Field(min_length=1, max_length=255)


class ExecutionSubmitRequest(HTTPModel):
    idempotency_key: str = Field(min_length=1, max_length=255)
    mode: ExecutionMode
    trigger_type: TriggerType = TriggerType.INTERACTIVE
    runtime_type: RuntimeType = RuntimeType.JUPYTER
    runtime_profile: str = Field(min_length=1, max_length=128)
    source: CodeSource
    context: ExecutionSubmitContext
    actor: ActorInput
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
            runtime_type=self.runtime_type,
            runtime_profile=self.runtime_profile,
            code_source_type=self.source.type,
            source_content=source_content,
            code_path=self.source.path if isinstance(self.source, PathCodeSource) else None,
            source_sha256=source_sha256,
            user_id=self.context.user_id,
            project_id=self.context.project_id,
            session_id=self.context.session_id,
            task_id=self.context.task_id,
            execution_plan_id=spec.execution_plan_id,
            actor_type=self.actor.type,
            actor_id=self.actor.id,
            workflow_id=self.context.workflow_id,
            metadata=self.metadata,
            steps=tuple(
                StepSpec(
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


class ExecutionCancelRequest(HTTPModel):
    idempotency_key: str = Field(min_length=1, max_length=255)
    reason: str | None = Field(default=None, max_length=2000)
    actor: ActorInput


class ExecutionRetryRequest(HTTPModel):
    idempotency_key: str = Field(min_length=1, max_length=255)
    actor: ActorInput


class ExecutionContinueRequest(HTTPModel):
    idempotency_key: str = Field(min_length=1, max_length=255)
    expected_version: int = Field(ge=0)
    source: CodeSource
    actor: ActorInput


class ExecutionFinishRequest(HTTPModel):
    idempotency_key: str = Field(min_length=1, max_length=255)
    expected_version: int = Field(ge=0)
    actor: ActorInput


class ExecutionStepResponse(HTTPModel):
    step_id: UUID
    sequence: int
    code_hash: str | None
    execution_plan_id: str
    plan_step_id: str
    skill_name: str | None
    tool_name: str | None
    status: StepStatus
    outputs: list[dict[str, Any]]
    error_message: str | None
    created_by_type: ActorType | None
    created_by: str | None
    updated_by_type: ActorType | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def from_domain(cls, step: ExecutionStep) -> "ExecutionStepResponse":
        return cls(
            step_id=step.id,
            sequence=step.sequence,
            code_hash=step.code_hash,
            execution_plan_id=step.execution_plan_id,
            plan_step_id=step.plan_step_id,
            skill_name=step.skill_name,
            tool_name=step.tool_name,
            status=step.status,
            outputs=step.outputs,
            error_message=step.error_message,
            created_by_type=step.created_by_type,
            created_by=step.created_by,
            updated_by_type=step.updated_by_type,
            updated_by=step.updated_by,
            created_at=step.created_at,
            updated_at=step.updated_at,
            started_at=step.started_at,
            finished_at=step.finished_at,
        )


class ExecutionSourceResponse(HTTPModel):
    type: CodeSourceType
    path: str | None
    sha256: str


class ExecutionResponse(HTTPModel):
    execution_id: UUID
    status: ExecutionStatus
    mode: ExecutionMode
    trigger_type: TriggerType
    runtime_type: RuntimeType
    runtime_pool: RuntimePool
    runtime_profile: str
    source: ExecutionSourceResponse
    context: ExecutionContext
    steps: list[ExecutionStepResponse]
    cancellation_reason: str | None
    runtime_target_id: UUID | None
    runtime_session_id: str | None
    workspace_path: str | None
    notebook_path: str | None
    error_message: str | None
    failure_type: FailureType | None
    retry_strategy: RetryStrategy
    recovery_count: int
    runtime_session_cleanup_status: RuntimeSessionCleanupStatus
    version: int
    created_by_type: ActorType | None
    created_by: str | None
    updated_by_type: ActorType | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    retry_from_sequence: int | None
    retained_runtime_session_until: datetime | None
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
            runtime_type=execution.runtime_type,
            runtime_pool=execution.runtime_pool,
            runtime_profile=execution.runtime_profile,
            source=ExecutionSourceResponse(
                type=execution.code_source_type,
                path=execution.code_path,
                sha256=execution.source_sha256,
            ),
            context=ExecutionContext(
                user_id=execution.user_id,
                project_id=execution.project_id,
                session_id=execution.session_id,
                task_id=execution.task_id,
                execution_plan_id=execution.execution_plan_id,
                workflow_id=execution.workflow_id,
            ),
            steps=[ExecutionStepResponse.from_domain(step) for step in execution.steps],
            cancellation_reason=execution.cancellation_reason,
            runtime_target_id=execution.runtime_target_id,
            runtime_session_id=execution.runtime_session_id,
            workspace_path=execution.workspace_path,
            notebook_path=execution.notebook_path,
            error_message=execution.error_message,
            failure_type=execution.failure_type,
            retry_strategy=execution.retry_strategy,
            recovery_count=execution.recovery_count,
            runtime_session_cleanup_status=execution.runtime_session_cleanup_status,
            version=execution.version,
            created_by_type=execution.created_by_type,
            created_by=execution.created_by,
            updated_by_type=execution.updated_by_type,
            updated_by=execution.updated_by,
            created_at=execution.created_at,
            updated_at=execution.updated_at,
            started_at=execution.started_at,
            finished_at=execution.finished_at,
            retry_from_sequence=execution.retry_from_sequence,
            retained_runtime_session_until=execution.retained_runtime_session_until,
            retry_count=execution.retry_count,
            dynamic_wait_expires_at=execution.dynamic_wait_expires_at,
            execution_expires_at=execution.execution_expires_at,
        )


class ExecutionSummaryResponse(HTTPModel):
    execution_id: UUID
    status: ExecutionStatus
    mode: ExecutionMode
    trigger_type: TriggerType
    runtime_type: RuntimeType
    runtime_pool: RuntimePool
    runtime_profile: str
    context: ExecutionContext
    step_count: int
    error_message: str | None
    failure_type: FailureType | None
    retry_strategy: RetryStrategy
    retry_count: int
    version: int
    created_by_type: ActorType | None
    created_by: str | None
    updated_by_type: ActorType | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def from_domain(cls, execution: Execution) -> "ExecutionSummaryResponse":
        return cls(
            execution_id=execution.id,
            status=execution.status,
            mode=execution.mode,
            trigger_type=execution.trigger_type,
            runtime_type=execution.runtime_type,
            runtime_pool=execution.runtime_pool,
            runtime_profile=execution.runtime_profile,
            context=ExecutionContext(
                user_id=execution.user_id,
                project_id=execution.project_id,
                session_id=execution.session_id,
                task_id=execution.task_id,
                execution_plan_id=execution.execution_plan_id,
                workflow_id=execution.workflow_id,
            ),
            step_count=len(execution.steps),
            error_message=execution.error_message,
            failure_type=execution.failure_type,
            retry_strategy=execution.retry_strategy,
            retry_count=execution.retry_count,
            version=execution.version,
            created_by_type=execution.created_by_type,
            created_by=execution.created_by,
            updated_by_type=execution.updated_by_type,
            updated_by=execution.updated_by,
            created_at=execution.created_at,
            updated_at=execution.updated_at,
            started_at=execution.started_at,
            finished_at=execution.finished_at,
        )


class ExecutionPageResponse(HTTPModel):
    items: list[ExecutionSummaryResponse]
    next_cursor: str | None
    has_more: bool

    @classmethod
    def from_page(cls, page: Page[Execution]) -> "ExecutionPageResponse":
        return cls(
            items=[ExecutionSummaryResponse.from_domain(item) for item in page.items],
            next_cursor=page.next_cursor,
            has_more=page.next_cursor is not None,
        )


class ExecutionStepAttemptResponse(HTTPModel):
    step_attempt_id: UUID
    execution_step_id: UUID
    sequence: int
    skill_name: str | None
    tool_name: str | None
    input_parameters: dict[str, Any]
    status: StepStatus
    outputs: list[dict[str, Any]]
    error_message: str | None
    created_by_type: ActorType | None
    created_by: str | None
    updated_by_type: ActorType | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime
    finished_at: datetime | None

    @classmethod
    def from_view(cls, view: ExecutionStepAttemptView) -> "ExecutionStepAttemptResponse":
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
            created_by_type=view.created_by_type,
            created_by=view.created_by,
            updated_by_type=view.updated_by_type,
            updated_by=view.updated_by,
            created_at=view.created_at,
            updated_at=view.updated_at,
            started_at=view.started_at,
            finished_at=view.finished_at,
        )


class ExecutionAttemptResponse(HTTPModel):
    attempt_id: UUID
    execution_id: UUID
    attempt_number: int
    runtime_type: RuntimeType
    runtime_profile: str
    runtime_target_id: UUID
    runtime_session_id: str | None
    status: AttemptStatus
    lease_owner: str | None
    lease_expires_at: datetime | None
    heartbeat_at: datetime
    error_message: str | None
    failure_type: FailureType | None
    retry_strategy: RetryStrategy
    runtime_session_cleanup_status: RuntimeSessionCleanupStatus
    created_by_type: ActorType | None
    created_by: str | None
    updated_by_type: ActorType | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime
    finished_at: datetime | None
    steps: list[ExecutionStepAttemptResponse]

    @classmethod
    def from_view(cls, view: ExecutionAttemptView) -> "ExecutionAttemptResponse":
        return cls(
            attempt_id=view.id,
            execution_id=view.execution_id,
            attempt_number=view.attempt_number,
            runtime_type=view.runtime_type,
            runtime_profile=view.runtime_profile,
            runtime_target_id=view.runtime_target_id,
            runtime_session_id=view.runtime_session_id,
            status=view.status,
            lease_owner=view.lease_owner,
            lease_expires_at=view.lease_expires_at,
            heartbeat_at=view.heartbeat_at,
            error_message=view.error_message,
            failure_type=view.failure_type,
            retry_strategy=view.retry_strategy,
            runtime_session_cleanup_status=view.runtime_session_cleanup_status,
            created_by_type=view.created_by_type,
            created_by=view.created_by,
            updated_by_type=view.updated_by_type,
            updated_by=view.updated_by,
            created_at=view.created_at,
            updated_at=view.updated_at,
            started_at=view.started_at,
            finished_at=view.finished_at,
            steps=[ExecutionStepAttemptResponse.from_view(step) for step in view.steps],
        )


class ExecutionStepPageResponse(HTTPModel):
    items: list[ExecutionStepResponse]
    next_cursor: str | None
    has_more: bool

    @classmethod
    def from_page(cls, page: Page[ExecutionStep]) -> "ExecutionStepPageResponse":
        return cls(
            items=[ExecutionStepResponse.from_domain(item) for item in page.items],
            next_cursor=page.next_cursor,
            has_more=page.next_cursor is not None,
        )


class ExecutionAttemptPageResponse(HTTPModel):
    items: list[ExecutionAttemptResponse]
    next_cursor: str | None
    has_more: bool

    @classmethod
    def from_page(cls, page: Page[ExecutionAttemptView]) -> "ExecutionAttemptPageResponse":
        return cls(
            items=[ExecutionAttemptResponse.from_view(item) for item in page.items],
            next_cursor=page.next_cursor,
            has_more=page.next_cursor is not None,
        )


class ExecutionEventResponse(HTTPModel):
    event_id: UUID
    event_type: str
    payload: dict[str, Any]
    delivery_status: OutboxStatus
    publish_attempt_count: int
    created_by_type: ActorType | None
    created_by: str | None
    updated_by_type: ActorType | None
    updated_by: str | None
    available_at: datetime
    created_at: datetime
    updated_at: datetime
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
            created_by_type=view.created_by_type,
            created_by=view.created_by,
            updated_by_type=view.updated_by_type,
            updated_by=view.updated_by,
            available_at=view.available_at,
            created_at=view.created_at,
            updated_at=view.updated_at,
            published_at=view.published_at,
            last_error=view.last_error,
        )


class ExecutionEventPageResponse(HTTPModel):
    items: list[ExecutionEventResponse]
    next_cursor: str | None
    has_more: bool

    @classmethod
    def from_page(cls, page: Page[ExecutionEventView]) -> "ExecutionEventPageResponse":
        return cls(
            items=[ExecutionEventResponse.from_view(item) for item in page.items],
            next_cursor=page.next_cursor,
            has_more=page.next_cursor is not None,
        )


class ExecutionArtifactResponse(HTTPModel):
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
    created_by_type: ActorType | None
    created_by: str | None
    updated_by_type: ActorType | None
    updated_by: str | None
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
            created_by_type=view.created_by_type,
            created_by=view.created_by,
            updated_by_type=view.updated_by_type,
            updated_by=view.updated_by,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )


class ExecutionArtifactPageResponse(HTTPModel):
    items: list[ExecutionArtifactResponse]
    next_cursor: str | None
    has_more: bool

    @classmethod
    def from_page(cls, page: Page[ExecutionArtifactView]) -> "ExecutionArtifactPageResponse":
        return cls(
            items=[ExecutionArtifactResponse.from_view(item) for item in page.items],
            next_cursor=page.next_cursor,
            has_more=page.next_cursor is not None,
        )


class ExecutionTraceResponse(HTTPModel):
    execution: ExecutionResponse
    attempts: ExecutionAttemptPageResponse
    events: ExecutionEventPageResponse
    artifacts: ExecutionArtifactPageResponse

    @classmethod
    def from_view(cls, view: ExecutionTraceView) -> "ExecutionTraceResponse":
        return cls(
            execution=ExecutionResponse.from_domain(view.execution),
            attempts=ExecutionAttemptPageResponse.from_page(view.attempts),
            events=ExecutionEventPageResponse.from_page(view.events),
            artifacts=ExecutionArtifactPageResponse.from_page(view.artifacts),
        )


class ExecutorCapabilitiesResponse(HTTPModel):
    service: str = "executor-service"
    api_version: str = "v1"
    mcp_endpoint: str = "/mcp"
    mcp_protocol_revision: str = "2026-07-28"
    mcp_tasks_supported: bool = False
    execution_modes: tuple[ExecutionMode, ...] = (
        ExecutionMode.STATIC,
        ExecutionMode.DYNAMIC,
    )
    code_source_types: tuple[CodeSourceType, ...] = (
        CodeSourceType.INLINE,
        CodeSourceType.PATH,
    )
    runtime_types: tuple[RuntimeType, ...] = tuple(RuntimeType)
    runtime_pools: tuple[RuntimePool, ...] = (
        RuntimePool.INTERACTIVE,
        RuntimePool.BATCH,
    )
    event_delivery: str = "redis-streams-via-transactional-outbox"
    failure_types: tuple[FailureType, ...] = tuple(FailureType)
    retry_strategies: tuple[RetryStrategy, ...] = tuple(RetryStrategy)


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
