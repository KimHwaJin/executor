"""Transport-shared Pydantic contracts for REST responses and MCP structured content."""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from executor_service.application.commands import (
    MaterializeArtifactCommand,
    StepSpec,
    SubmitExecutionCommand,
)
from executor_service.application.execution_queries import (
    ExecutionArtifactView,
    ExecutionAttemptView,
    ExecutionDetailView,
    ExecutionEventView,
    ExecutionOperationView,
    ExecutionStepAttemptView,
    ExecutionSummaryView,
)
from executor_service.application.execution_results import (
    AttemptResultBundle,
    ExecutionResultBundle,
    OperationResultBundle,
)
from executor_service.application.notebook_queries import (
    NotebookCellView,
    NotebookView,
)
from executor_service.application.pagination import Page
from executor_service.application.runtime_targets import (
    RuntimePoolView,
    RuntimeTargetView,
    UpsertRuntimeTargetCommand,
)
from executor_service.domain.enums import (
    ActorType,
    ArtifactStatus,
    ArtifactStorageType,
    ArtifactType,
    AttemptStatus,
    CodeSourceType,
    ExecutionStatus,
    FailureType,
    OperationMode,
    OperationStatus,
    OutboxStatus,
    RetryStrategy,
    RuntimeAbortStatus,
    RuntimePool,
    RuntimeSessionCleanupStatus,
    RuntimeTargetStatus,
    RuntimeType,
    StepStatus,
    TriggerType,
)
from executor_service.domain.models import (
    Execution,
    ExecutionStep,
    NotebookProjectionStatus,
)
from executor_service.execution_specs import (
    ExecutionSpec,
    ResolvedExecutionSpec,
)
from executor_service.result_summaries import OutputSummary


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AuditFields(ContractModel):
    created_by_type: ActorType | None
    created_by: str | None
    updated_by_type: ActorType | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime


class ActorInput(ContractModel):
    type: ActorType
    id: str = Field(min_length=1, max_length=255)


class InlineArtifactSource(ContractModel):
    type: Literal[CodeSourceType.INLINE]
    content: str = Field(min_length=1)


class PathArtifactSource(ContractModel):
    type: Literal[CodeSourceType.PATH]
    path: str = Field(min_length=1, max_length=4096)
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")


ArtifactSource = Annotated[
    InlineArtifactSource | PathArtifactSource, Field(discriminator="type")
]


class ExecutionArtifactMaterializeRequest(ContractModel):
    idempotency_key: str = Field(min_length=1, max_length=255)
    type: ArtifactType
    source: ArtifactSource
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4000)
    media_type: str | None = Field(default=None, min_length=1, max_length=255)
    append_to_notebook: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    actor: ActorInput

    @model_validator(mode="after")
    def validate_notebook_append(
        self,
    ) -> "ExecutionArtifactMaterializeRequest":
        if self.append_to_notebook and self.type != ArtifactType.REPORT:
            raise ValueError(
                "append_to_notebook is supported only for REPORT Artifacts."
            )
        return self

    def to_command(self, execution_id: UUID) -> MaterializeArtifactCommand:
        return MaterializeArtifactCommand(
            execution_id=execution_id,
            idempotency_key=self.idempotency_key,
            artifact_type=self.type,
            source_type=self.source.type,
            source_content=(
                self.source.content
                if isinstance(self.source, InlineArtifactSource)
                else None
            ),
            source_path=(
                self.source.path
                if isinstance(self.source, PathArtifactSource)
                else None
            ),
            source_sha256=(
                self.source.sha256
                if isinstance(self.source, PathArtifactSource)
                else None
            ),
            name=self.name,
            description=self.description,
            media_type=self.media_type,
            append_to_notebook=self.append_to_notebook,
            metadata=self.metadata,
            actor_type=self.actor.type,
            actor_id=self.actor.id,
        )


class NotebookCellResponse(ContractModel):
    index: int
    id: str | None
    type: str
    execution_count: int | None
    source: str
    line_count: int
    metadata: dict[str, Any]
    output_summary: OutputSummary
    outputs: list[dict[str, Any]]

    @classmethod
    def from_view(cls, view: NotebookCellView) -> "NotebookCellResponse":
        return cls(
            **{field: getattr(view, field) for field in cls.model_fields}
        )


class NotebookPage(ContractModel):
    start_index: int
    limit: int
    total_count: int
    has_more: bool


class ExecutionNotebookResponse(ContractModel):
    execution_id: UUID
    response_format: str
    metadata: dict[str, Any]
    cells: list[NotebookCellResponse]
    page: NotebookPage

    @classmethod
    def from_view(cls, view: NotebookView) -> "ExecutionNotebookResponse":
        return cls(
            execution_id=view.execution_id,
            response_format=view.response_format,
            metadata=view.metadata,
            cells=[
                NotebookCellResponse.from_view(cell) for cell in view.cells
            ],
            page=NotebookPage(
                start_index=view.start_index,
                limit=view.limit,
                total_count=view.total_count,
                has_more=view.start_index + len(view.cells) < view.total_count,
            ),
        )


class ExecutionNotebookCellResponse(ContractModel):
    execution_id: UUID
    cell: NotebookCellResponse

    @classmethod
    def from_view(
        cls, execution_id: UUID, view: NotebookCellView
    ) -> "ExecutionNotebookCellResponse":
        return cls(
            execution_id=execution_id,
            cell=NotebookCellResponse.from_view(view),
        )


class ExecutionContext(ContractModel):
    user_id: str = Field(min_length=1, max_length=255)
    task_id: str = Field(min_length=1, max_length=255)
    project_id: str | None = Field(default=None, min_length=1, max_length=255)
    session_id: str | None = Field(default=None, min_length=1, max_length=255)
    workflow_id: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def validate_scope(self) -> "ExecutionContext":
        if self.session_id is not None and self.project_id is None:
            raise ValueError("context.session_id requires context.project_id.")
        if self.project_id == "unscoped" or self.session_id == "unscoped":
            raise ValueError(
                "'unscoped' is reserved for Executor workspace paths."
            )
        return self


class ExecutionLifecycleInput(ContractModel):
    operation_mode: OperationMode
    operation_wait_timeout_seconds: int | None = Field(default=None, ge=30)

    @model_validator(mode="after")
    def validate_wait_timeout(self) -> "ExecutionLifecycleInput":
        if self.operation_mode == OperationMode.MULTI:
            if self.operation_wait_timeout_seconds is None:
                raise ValueError(
                    "MULTI lifecycle requires operation_wait_timeout_seconds."
                )
        elif self.operation_wait_timeout_seconds is not None:
            raise ValueError(
                "SINGLE lifecycle does not accept operation_wait_timeout_seconds."
            )
        return self


class ExecutionTriggerInput(ContractModel):
    type: TriggerType
    actor: ActorInput


class ExecutionRuntimeInput(ContractModel):
    type: RuntimeType
    profile: str = Field(min_length=1, max_length=128)


class ExecutionOperationInput(ContractModel):
    operation_timeout_seconds: int | None = Field(default=None, ge=1)
    spec: ExecutionSpec
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExecutionSubmitRequest(ContractModel):
    idempotency_key: str = Field(min_length=1, max_length=255)
    lifecycle: ExecutionLifecycleInput
    trigger: ExecutionTriggerInput
    runtime: ExecutionRuntimeInput
    context: ExecutionContext
    operation: ExecutionOperationInput
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_command(
        self, resolved: ResolvedExecutionSpec
    ) -> SubmitExecutionCommand:
        return SubmitExecutionCommand(
            idempotency_key=self.idempotency_key,
            operation_mode=self.lifecycle.operation_mode,
            operation_wait_timeout_seconds=self.lifecycle.operation_wait_timeout_seconds,
            trigger_type=self.trigger.type,
            runtime_type=self.runtime.type,
            runtime_profile=self.runtime.profile,
            user_id=self.context.user_id,
            project_id=self.context.project_id,
            session_id=self.context.session_id,
            task_id=self.context.task_id,
            spec_schema_version=resolved.spec.schema_version,
            operation_timeout_seconds=self.operation.operation_timeout_seconds,
            actor_type=self.trigger.actor.type,
            actor_id=self.trigger.actor.id,
            workflow_id=self.context.workflow_id,
            metadata=self.metadata,
            operation_metadata=self.operation.metadata,
            steps=tuple(
                StepSpec(
                    sequence=step.sequence,
                    code=step.content,
                    source_type=step.source_type,
                    source_path=step.source_path,
                    source_sha256=step.source_sha256,
                    step_timeout_seconds=step.step_timeout_seconds,
                    skill_name=step.skill_name,
                    tool_name=step.tool_name,
                    input_parameters=step.input_parameters,
                )
                for step in resolved.steps
            ),
        )


class RuntimeTargetUpsertRequest(ContractModel):
    idempotency_key: str = Field(min_length=1, max_length=255)
    name: str = Field(
        min_length=1, max_length=255, pattern=r"^[a-zA-Z0-9._-]+$"
    )
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
            if set(self.connection_config) != {"endpoint"} or not isinstance(
                endpoint, str
            ):
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
            credential=(
                self.credential.get_secret_value() if self.credential else None
            ),
            pool=self.pool,
            max_concurrent_executions=self.max_concurrent_executions,
            actor_type=self.actor.type,
            actor_id=self.actor.id,
        )


class RuntimeTargetRuntime(ContractModel):
    type: RuntimeType
    pool: RuntimePool
    connection_config: dict[str, Any]
    supported_profiles: list[str]


class RuntimeTargetState(ContractModel):
    status: RuntimeTargetStatus
    enabled: bool
    accepting_new_executions: bool
    drain_complete: bool


class RuntimeTargetCapacity(ContractModel):
    max_concurrent_executions: int
    active_execution_count: int
    active_session_count: int | None
    admission_used_count: int
    available_capacity: int
    admission_blocked: bool
    session_count_observed_at: datetime | None
    session_count_fresh: bool


class RuntimeTargetHealth(ContractModel):
    last_check_at: datetime | None
    last_error: str | None


class CpuResources(ContractModel):
    used_cores: float | None
    capacity_cores: float | None
    utilization: float | None


class MemoryResources(ContractModel):
    used_bytes: int | None
    capacity_bytes: int | None
    utilization: float | None


class RuntimeTargetResources(ContractModel):
    observed_at: datetime | None
    last_check_at: datetime | None
    last_error: str | None
    fresh: bool
    source: str | None
    estimated: bool | None
    process_count: int | None
    pressure_score: float | None
    cpu: CpuResources
    memory: MemoryResources
    errors: list[str]


class RuntimeTargetResponse(AuditFields):
    target_id: UUID
    name: str
    runtime: RuntimeTargetRuntime
    state: RuntimeTargetState
    capacity: RuntimeTargetCapacity
    health: RuntimeTargetHealth
    resources: RuntimeTargetResources

    @classmethod
    def from_view(cls, view: RuntimeTargetView) -> "RuntimeTargetResponse":
        return cls(
            target_id=view.id,
            name=view.name,
            runtime=RuntimeTargetRuntime(
                type=view.runtime_type,
                pool=view.pool,
                connection_config=view.connection_config,
                supported_profiles=list(view.supported_profiles),
            ),
            state=RuntimeTargetState(
                status=view.status,
                enabled=view.enabled,
                accepting_new_executions=view.accepting_new_executions,
                drain_complete=view.drain_complete,
            ),
            capacity=RuntimeTargetCapacity(
                max_concurrent_executions=view.max_concurrent_executions,
                active_execution_count=view.active_execution_count,
                active_session_count=view.active_session_count,
                admission_used_count=view.admission_used_count,
                available_capacity=view.available_capacity,
                admission_blocked=view.admission_blocked,
                session_count_observed_at=view.session_count_observed_at,
                session_count_fresh=view.session_count_fresh,
            ),
            health=RuntimeTargetHealth(
                last_check_at=view.last_health_check_at,
                last_error=view.last_health_error,
            ),
            resources=RuntimeTargetResources(
                observed_at=view.resource_observed_at,
                last_check_at=view.resource_last_check_at,
                last_error=view.resource_last_error,
                fresh=view.resource_fresh,
                source=view.resource_source,
                estimated=view.resource_estimated,
                process_count=view.resource_process_count,
                pressure_score=view.resource_pressure_score,
                cpu=CpuResources(
                    used_cores=view.cpu_used_cores,
                    capacity_cores=view.cpu_capacity_cores,
                    utilization=view.cpu_utilization,
                ),
                memory=MemoryResources(
                    used_bytes=view.memory_used_bytes,
                    capacity_bytes=view.memory_capacity_bytes,
                    utilization=view.memory_utilization,
                ),
                errors=list(view.resource_errors),
            ),
            created_by_type=view.created_by_type,
            created_by=view.created_by,
            updated_by_type=view.updated_by_type,
            updated_by=view.updated_by,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )


class PageResponse(ContractModel):
    next_cursor: str | None
    has_more: bool


class RuntimeTargetPageResponse(PageResponse):
    items: list[RuntimeTargetResponse]

    @classmethod
    def from_page(
        cls, page: Page[RuntimeTargetView]
    ) -> "RuntimeTargetPageResponse":
        return cls(
            items=[
                RuntimeTargetResponse.from_view(item) for item in page.items
            ],
            next_cursor=page.next_cursor,
            has_more=page.next_cursor is not None,
        )


class RuntimePoolIdentity(ContractModel):
    type: RuntimeType
    pool: RuntimePool


class RuntimePoolTargets(ContractModel):
    total: int
    enabled: int
    active: int
    draining: int
    offline: int


class RuntimePoolCapacity(ContractModel):
    configured: int
    schedulable: int
    reserved_execution_count: int
    available: int


class RuntimePoolState(ContractModel):
    accepting_new_executions: bool
    saturated: bool


class RuntimePoolResponse(ContractModel):
    runtime: RuntimePoolIdentity
    targets: RuntimePoolTargets
    capacity: RuntimePoolCapacity
    state: RuntimePoolState

    @classmethod
    def from_view(cls, view: RuntimePoolView) -> "RuntimePoolResponse":
        return cls(
            runtime=RuntimePoolIdentity(
                type=view.runtime_type, pool=view.pool
            ),
            targets=RuntimePoolTargets(
                total=view.target_count,
                enabled=view.enabled_target_count,
                active=view.active_target_count,
                draining=view.draining_target_count,
                offline=view.offline_target_count,
            ),
            capacity=RuntimePoolCapacity(
                configured=view.configured_capacity,
                schedulable=view.schedulable_capacity,
                reserved_execution_count=view.active_execution_count,
                available=view.available_capacity,
            ),
            state=RuntimePoolState(
                accepting_new_executions=view.accepting_new_executions,
                saturated=view.saturated,
            ),
        )


class RuntimePoolPageResponse(ContractModel):
    items: list[RuntimePoolResponse]


class ToolReference(ContractModel):
    skill_name: str | None
    tool_name: str | None
    input_parameters: dict[str, Any]


class StepResultReference(ContractModel):
    """Immutable Step result manifest in the Agent/Executor shared volume."""

    storage: Literal["SHARED_PV"] = "SHARED_PV"
    execution_id: UUID
    step_id: UUID
    attempt_id: UUID
    fencing_token: int
    relative_path: str
    checksum_sha256: str
    complete: bool
    representation_count: int
    total_size_bytes: int


class StepResultSummary(ContractModel):
    status: StepStatus
    output_summary: OutputSummary
    error_message: str | None


class StepResult(StepResultSummary):
    result_ref: StepResultReference | None


class Lifecycle(ContractModel):
    started_at: datetime | None
    finished_at: datetime | None


class ExecutionStepResponse(AuditFields):
    step_id: UUID
    execution_id: UUID
    sequence: int
    code_hash: str | None
    source: "StepSourceResponse"
    step_timeout_seconds: int | None
    lineage: ToolReference
    result: StepResult
    lifecycle: Lifecycle

    @classmethod
    def from_domain(
        cls, step: ExecutionStep, execution_id: UUID
    ) -> "ExecutionStepResponse":
        return cls(
            step_id=step.id,
            execution_id=execution_id,
            sequence=step.sequence,
            code_hash=step.code_hash,
            source=StepSourceResponse(
                type=step.source_type,
                path=step.source_path,
                sha256=step.source_sha256,
            ),
            step_timeout_seconds=step.step_timeout_seconds,
            lineage=ToolReference(
                skill_name=step.skill_name,
                tool_name=step.tool_name,
                input_parameters=step.input_parameters,
            ),
            result=StepResult(
                status=step.status,
                output_summary=OutputSummary.model_validate(
                    step.output_summary
                ),
                result_ref=(
                    StepResultReference(
                        execution_id=execution_id,
                        step_id=step.id,
                        attempt_id=step.result_execution_attempt_id,
                        fencing_token=step.result_fencing_token,
                        relative_path=step.result_manifest_path,
                        checksum_sha256=(step.result_manifest_checksum_sha256),
                        complete=step.result_complete,
                        representation_count=(
                            step.result_representation_count
                        ),
                        total_size_bytes=step.result_total_size_bytes,
                    )
                    if (
                        step.result_execution_attempt_id is not None
                        and step.result_fencing_token is not None
                        and step.result_manifest_path is not None
                        and step.result_manifest_checksum_sha256 is not None
                        and step.result_complete is not None
                    )
                    else None
                ),
                error_message=step.error_message,
            ),
            lifecycle=Lifecycle(
                started_at=step.started_at, finished_at=step.finished_at
            ),
            created_by_type=step.created_by_type,
            created_by=step.created_by,
            updated_by_type=step.updated_by_type,
            updated_by=step.updated_by,
            created_at=step.created_at,
            updated_at=step.updated_at,
        )


class ExecutionStepSummaryResponse(AuditFields):
    step_id: UUID
    execution_id: UUID
    sequence: int
    lineage: ToolReference
    result: StepResultSummary
    lifecycle: Lifecycle

    @classmethod
    def from_domain(
        cls, step: ExecutionStep, execution_id: UUID
    ) -> "ExecutionStepSummaryResponse":
        return cls(
            step_id=step.id,
            execution_id=execution_id,
            sequence=step.sequence,
            lineage=ToolReference(
                skill_name=step.skill_name,
                tool_name=step.tool_name,
                input_parameters=step.input_parameters,
            ),
            result=StepResultSummary(
                status=step.status,
                output_summary=OutputSummary.model_validate(
                    step.output_summary
                ),
                error_message=step.error_message,
            ),
            lifecycle=Lifecycle(
                started_at=step.started_at, finished_at=step.finished_at
            ),
            created_by_type=step.created_by_type,
            created_by=step.created_by,
            updated_by_type=step.updated_by_type,
            updated_by=step.updated_by,
            created_at=step.created_at,
            updated_at=step.updated_at,
        )


class StepSourceResponse(ContractModel):
    type: CodeSourceType
    path: str | None
    sha256: str


class ExecutionRuntime(ContractModel):
    type: RuntimeType
    pool: RuntimePool
    profile: str
    target_id: UUID | None
    session_id: str | None


class ExecutionState(ContractModel):
    status: ExecutionStatus
    version: int
    cancellation_reason: str | None


class ExecutionCommandState(ContractModel):
    status: ExecutionStatus
    version: int


class NotebookProjectionResponse(ContractModel):
    status: NotebookProjectionStatus
    attempt_count: int
    error_message: str | None
    projected_at: datetime | None


class WorkspaceResponse(ContractModel):
    path: str | None
    notebook_path: str | None
    notebook_projection: NotebookProjectionResponse


class FailureResponse(ContractModel):
    type: FailureType
    message: str


class RetryResponse(ContractModel):
    strategy: RetryStrategy
    count: int
    from_sequence: int | None
    retained_runtime_session_until: datetime | None


class RecoveryResponse(ContractModel):
    count: int
    runtime_session_cleanup_status: RuntimeSessionCleanupStatus
    runtime_abort_status: RuntimeAbortStatus


class DeadlinesResponse(ContractModel):
    operation_wait_expires_at: datetime | None
    execution_expires_at: datetime | None


class ExecutionLifecycleResponse(ContractModel):
    operation_mode: OperationMode
    operation_wait_timeout_seconds: int | None
    started_at: datetime | None
    finished_at: datetime | None


def _execution_common(
    execution: Execution | ExecutionDetailView,
) -> dict[str, Any]:
    failure = None
    if (
        execution.failure_type is not None
        and execution.error_message is not None
    ):
        failure = FailureResponse(
            type=execution.failure_type, message=execution.error_message
        )
    return {
        "execution_id": execution.id,
        "lifecycle": ExecutionLifecycleResponse(
            operation_mode=execution.operation_mode,
            operation_wait_timeout_seconds=execution.operation_wait_timeout_seconds,
            started_at=execution.started_at,
            finished_at=execution.finished_at,
        ),
        "trigger_type": execution.trigger_type,
        "context": ExecutionContext(
            user_id=execution.user_id,
            project_id=execution.project_id,
            session_id=execution.session_id,
            task_id=execution.task_id,
            workflow_id=execution.workflow_id,
        ),
        "runtime": ExecutionRuntime(
            type=execution.runtime_type,
            pool=execution.runtime_pool,
            profile=execution.runtime_profile,
            target_id=execution.runtime_target_id,
            session_id=execution.runtime_session_id,
        ),
        "state": ExecutionState(
            status=execution.status,
            version=execution.version,
            cancellation_reason=execution.cancellation_reason,
        ),
        "failure": failure,
        "retry": RetryResponse(
            strategy=execution.retry_strategy,
            count=execution.retry_count,
            from_sequence=execution.retry_from_sequence,
            retained_runtime_session_until=execution.retained_runtime_session_until,
        ),
        "created_by_type": execution.created_by_type,
        "created_by": execution.created_by,
        "updated_by_type": execution.updated_by_type,
        "updated_by": execution.updated_by,
        "created_at": execution.created_at,
        "updated_at": execution.updated_at,
    }


class ExecutionStepReceipt(ContractModel):
    sequence: int
    step_id: UUID


class ExecutionOperationReceipt(ContractModel):
    operation_id: UUID
    steps: list[ExecutionStepReceipt]


class ExecutionCommandResponse(AuditFields):
    execution_id: UUID
    operation: "ExecutionOperationReceipt | None"
    state: ExecutionCommandState

    @classmethod
    def from_domain(
        cls, execution: Execution, *, operation_id: UUID | None = None
    ) -> "ExecutionCommandResponse":
        return cls(
            execution_id=execution.id,
            operation=(
                ExecutionOperationReceipt(
                    operation_id=operation_id,
                    steps=[
                        ExecutionStepReceipt(
                            sequence=step.sequence, step_id=step.id
                        )
                        for step in execution.steps
                        if step.operation_id == operation_id
                    ],
                )
                if operation_id is not None
                else None
            ),
            state=ExecutionCommandState(
                status=execution.status,
                version=execution.version,
            ),
            created_by_type=execution.created_by_type,
            created_by=execution.created_by,
            updated_by_type=execution.updated_by_type,
            updated_by=execution.updated_by,
            created_at=execution.created_at,
            updated_at=execution.updated_at,
        )


class ExecutionResponse(AuditFields):
    execution_id: UUID
    trigger_type: TriggerType
    context: ExecutionContext
    runtime: ExecutionRuntime
    state: ExecutionState
    workspace: WorkspaceResponse
    failure: FailureResponse | None
    retry: RetryResponse
    recovery: RecoveryResponse
    deadlines: DeadlinesResponse
    lifecycle: ExecutionLifecycleResponse

    @classmethod
    def from_view(
        cls, execution: ExecutionDetailView | Execution
    ) -> "ExecutionResponse":
        return cls(
            **_execution_common(execution),
            workspace=WorkspaceResponse(
                path=execution.workspace_path,
                notebook_path=execution.notebook_path,
                notebook_projection=NotebookProjectionResponse(
                    status=execution.notebook_projection_status,
                    attempt_count=(
                        execution.notebook_projection_attempt_count
                    ),
                    error_message=execution.notebook_projection_error,
                    projected_at=execution.notebook_projected_at,
                ),
            ),
            recovery=RecoveryResponse(
                count=execution.recovery_count,
                runtime_session_cleanup_status=execution.runtime_session_cleanup_status,
                runtime_abort_status=execution.runtime_abort_status,
            ),
            deadlines=DeadlinesResponse(
                operation_wait_expires_at=execution.operation_wait_expires_at,
                execution_expires_at=execution.execution_expires_at,
            ),
        )


class ExecutionSummaryResponse(AuditFields):
    execution_id: UUID
    operation_mode: OperationMode
    trigger_type: TriggerType
    context: ExecutionContext
    state: ExecutionCommandState
    lifecycle: Lifecycle
    step_count: int

    @classmethod
    def from_view(
        cls, execution: ExecutionSummaryView
    ) -> "ExecutionSummaryResponse":
        return cls(
            execution_id=execution.id,
            operation_mode=execution.operation_mode,
            trigger_type=execution.trigger_type,
            context=ExecutionContext(
                user_id=execution.user_id,
                project_id=execution.project_id,
                session_id=execution.session_id,
                task_id=execution.task_id,
                workflow_id=execution.workflow_id,
            ),
            state=ExecutionCommandState(
                status=execution.status,
                version=execution.version,
            ),
            lifecycle=Lifecycle(
                started_at=execution.started_at,
                finished_at=execution.finished_at,
            ),
            step_count=execution.step_count,
            created_by_type=execution.created_by_type,
            created_by=execution.created_by,
            updated_by_type=execution.updated_by_type,
            updated_by=execution.updated_by,
            created_at=execution.created_at,
            updated_at=execution.updated_at,
        )


class ExecutionPageResponse(PageResponse):
    items: list[ExecutionSummaryResponse]

    @classmethod
    def from_page(
        cls, page: Page[ExecutionSummaryView]
    ) -> "ExecutionPageResponse":
        return cls(
            items=[
                ExecutionSummaryResponse.from_view(item) for item in page.items
            ],
            next_cursor=page.next_cursor,
            has_more=page.next_cursor is not None,
        )


class ExecutionStepAttemptResponse(AuditFields):
    step_attempt_id: UUID
    execution_step_id: UUID
    sequence: int
    tool: ToolReference
    result: StepResult
    lifecycle: Lifecycle

    @classmethod
    def from_view(
        cls, view: ExecutionStepAttemptView
    ) -> "ExecutionStepAttemptResponse":
        return cls(
            step_attempt_id=view.id,
            execution_step_id=view.execution_step_id,
            sequence=view.sequence,
            tool=ToolReference(
                skill_name=view.skill_name,
                tool_name=view.tool_name,
                input_parameters=view.input_parameters,
            ),
            result=StepResult(
                status=view.status,
                output_summary=OutputSummary.model_validate(
                    view.output_summary
                ),
                result_ref=(
                    StepResultReference(
                        execution_id=view.execution_id,
                        step_id=view.execution_step_id,
                        attempt_id=view.execution_attempt_id,
                        fencing_token=view.result_fencing_token,
                        relative_path=view.result_manifest_path,
                        checksum_sha256=(view.result_manifest_checksum_sha256),
                        complete=view.result_complete,
                        representation_count=(
                            view.result_representation_count
                        ),
                        total_size_bytes=view.result_total_size_bytes,
                    )
                    if (
                        view.status
                        in {StepStatus.SUCCEEDED, StepStatus.FAILED}
                        and view.result_fencing_token is not None
                        and view.result_manifest_path is not None
                        and view.result_manifest_checksum_sha256 is not None
                        and view.result_complete is not None
                    )
                    else None
                ),
                error_message=view.error_message,
            ),
            lifecycle=Lifecycle(
                started_at=view.started_at, finished_at=view.finished_at
            ),
            created_by_type=view.created_by_type,
            created_by=view.created_by,
            updated_by_type=view.updated_by_type,
            updated_by=view.updated_by,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )


class ExecutionStepAttemptSummaryResponse(AuditFields):
    step_attempt_id: UUID
    execution_step_id: UUID
    sequence: int
    tool: ToolReference
    result: StepResultSummary
    lifecycle: Lifecycle

    @classmethod
    def from_view(
        cls, view: ExecutionStepAttemptView
    ) -> "ExecutionStepAttemptSummaryResponse":
        return cls(
            step_attempt_id=view.id,
            execution_step_id=view.execution_step_id,
            sequence=view.sequence,
            tool=ToolReference(
                skill_name=view.skill_name,
                tool_name=view.tool_name,
                input_parameters=view.input_parameters,
            ),
            result=StepResultSummary(
                status=view.status,
                output_summary=OutputSummary.model_validate(
                    view.output_summary
                ),
                error_message=view.error_message,
            ),
            lifecycle=Lifecycle(
                started_at=view.started_at, finished_at=view.finished_at
            ),
            created_by_type=view.created_by_type,
            created_by=view.created_by,
            updated_by_type=view.updated_by_type,
            updated_by=view.updated_by,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )


class AttemptState(ContractModel):
    status: AttemptStatus


class AttemptRuntime(ContractModel):
    type: RuntimeType
    profile: str
    target_id: UUID
    session_id: str | None


class AttemptLease(ContractModel):
    owner: str | None
    expires_at: datetime | None
    heartbeat_at: datetime | None


class AttemptRecovery(ContractModel):
    retry_strategy: RetryStrategy
    runtime_session_cleanup_status: RuntimeSessionCleanupStatus
    runtime_abort_status: RuntimeAbortStatus


class ExecutionAttemptResponse(AuditFields):
    attempt_id: UUID
    execution_id: UUID
    attempt_number: int
    state: AttemptState
    failure: FailureResponse | None
    lifecycle: Lifecycle
    step_count: int

    @classmethod
    def from_view(
        cls, view: ExecutionAttemptView
    ) -> "ExecutionAttemptResponse":
        failure = None
        if view.failure_type is not None and view.error_message is not None:
            failure = FailureResponse(
                type=view.failure_type, message=view.error_message
            )
        return cls(
            attempt_id=view.id,
            execution_id=view.execution_id,
            attempt_number=view.attempt_number,
            state=AttemptState(status=view.status),
            failure=failure,
            lifecycle=Lifecycle(
                started_at=view.started_at, finished_at=view.finished_at
            ),
            step_count=view.step_count,
            created_by_type=view.created_by_type,
            created_by=view.created_by,
            updated_by_type=view.updated_by_type,
            updated_by=view.updated_by,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )


class ExecutionAttemptDetailResponse(ExecutionAttemptResponse):
    runtime: AttemptRuntime
    lease: AttemptLease
    recovery: AttemptRecovery

    @classmethod
    def from_view(
        cls, view: ExecutionAttemptView
    ) -> "ExecutionAttemptDetailResponse":
        summary = ExecutionAttemptResponse.from_view(view)
        return cls(
            **summary.model_dump(),
            runtime=AttemptRuntime(
                type=view.runtime_type,
                profile=view.runtime_profile,
                target_id=view.runtime_target_id,
                session_id=view.runtime_session_id,
            ),
            lease=AttemptLease(
                owner=view.lease_owner,
                expires_at=view.lease_expires_at,
                heartbeat_at=view.heartbeat_at,
            ),
            recovery=AttemptRecovery(
                retry_strategy=view.retry_strategy,
                runtime_session_cleanup_status=view.runtime_session_cleanup_status,
                runtime_abort_status=view.runtime_abort_status,
            ),
        )


class ExecutionStepPageResponse(PageResponse):
    items: list[ExecutionStepSummaryResponse]

    @classmethod
    def from_page(
        cls, page: Page[ExecutionStep], execution_id: UUID
    ) -> "ExecutionStepPageResponse":
        return cls(
            items=[
                ExecutionStepSummaryResponse.from_domain(item, execution_id)
                for item in page.items
            ],
            next_cursor=page.next_cursor,
            has_more=page.next_cursor is not None,
        )


class ExecutionAttemptPageResponse(PageResponse):
    items: list[ExecutionAttemptResponse]

    @classmethod
    def from_page(
        cls, page: Page[ExecutionAttemptView]
    ) -> "ExecutionAttemptPageResponse":
        return cls(
            items=[
                ExecutionAttemptResponse.from_view(item) for item in page.items
            ],
            next_cursor=page.next_cursor,
            has_more=page.next_cursor is not None,
        )


class OperationSequenceRange(ContractModel):
    first: int
    last: int


class OperationResult(ContractModel):
    status: OperationStatus
    error_message: str | None


class ExecutionOperationResponse(AuditFields):
    operation_id: UUID
    execution_id: UUID
    operation_number: int
    schema_version: str
    sequence_range: OperationSequenceRange
    operation_timeout_seconds: int | None
    metadata: dict[str, Any]
    execution_attempt_id: UUID | None
    result: OperationResult
    lifecycle: Lifecycle
    step_count: int

    @classmethod
    def from_view(
        cls, view: ExecutionOperationView
    ) -> "ExecutionOperationResponse":
        return cls(
            operation_id=view.id,
            execution_id=view.execution_id,
            operation_number=view.operation_number,
            schema_version=view.schema_version,
            sequence_range=OperationSequenceRange(
                first=view.first_sequence, last=view.last_sequence
            ),
            operation_timeout_seconds=view.operation_timeout_seconds,
            metadata=view.metadata,
            execution_attempt_id=view.execution_attempt_id,
            result=OperationResult(
                status=view.status, error_message=view.error_message
            ),
            lifecycle=Lifecycle(
                started_at=view.started_at, finished_at=view.finished_at
            ),
            step_count=view.step_count,
            created_by_type=view.created_by_type,
            created_by=view.created_by,
            updated_by_type=view.updated_by_type,
            updated_by=view.updated_by,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )


class ExecutionOperationPageResponse(PageResponse):
    items: list[ExecutionOperationResponse]

    @classmethod
    def from_page(
        cls, page: Page[ExecutionOperationView]
    ) -> "ExecutionOperationPageResponse":
        return cls(
            items=[
                ExecutionOperationResponse.from_view(item)
                for item in page.items
            ],
            next_cursor=page.next_cursor,
            has_more=page.next_cursor is not None,
        )


class ExecutionStepAttemptPageResponse(PageResponse):
    items: list[ExecutionStepAttemptSummaryResponse]

    @classmethod
    def from_page(
        cls, page: Page[ExecutionStepAttemptView]
    ) -> "ExecutionStepAttemptPageResponse":
        return cls(
            items=[
                ExecutionStepAttemptSummaryResponse.from_view(item)
                for item in page.items
            ],
            next_cursor=page.next_cursor,
            has_more=page.next_cursor is not None,
        )


class EventDelivery(ContractModel):
    status: OutboxStatus
    attempt_count: int
    available_at: datetime
    published_at: datetime | None
    last_error: str | None


class ExecutionEventResponse(AuditFields):
    event_id: UUID
    event_type: str
    payload: dict[str, Any]
    delivery: EventDelivery

    @classmethod
    def from_view(cls, view: ExecutionEventView) -> "ExecutionEventResponse":
        return cls(
            event_id=view.id,
            event_type=view.event_type,
            payload=view.payload,
            delivery=EventDelivery(
                status=view.delivery_status,
                attempt_count=view.publish_attempt_count,
                available_at=view.available_at,
                published_at=view.published_at,
                last_error=view.last_error,
            ),
            created_by_type=view.created_by_type,
            created_by=view.created_by,
            updated_by_type=view.updated_by_type,
            updated_by=view.updated_by,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )


class ExecutionEventPageResponse(PageResponse):
    items: list[ExecutionEventResponse]

    @classmethod
    def from_page(
        cls, page: Page[ExecutionEventView]
    ) -> "ExecutionEventPageResponse":
        return cls(
            items=[
                ExecutionEventResponse.from_view(item) for item in page.items
            ],
            next_cursor=page.next_cursor,
            has_more=page.next_cursor is not None,
        )


class ArtifactProducer(ContractModel):
    execution_id: UUID
    execution_attempt_id: UUID | None
    execution_step_id: UUID | None
    execution_step_attempt_id: UUID | None


class ArtifactLineage(ContractModel):
    parent_artifact_id: UUID | None
    external_parent_asset_id: str | None


class ArtifactStorage(ContractModel):
    type: ArtifactStorageType
    uri: str
    relative_path: str | None
    media_type: str | None
    size_bytes: int | None
    checksum_sha256: str | None


class ExecutionArtifactResponse(AuditFields):
    artifact_id: UUID
    name: str
    description: str | None
    type: ArtifactType
    status: ArtifactStatus
    produced_by: ArtifactProducer
    lineage: ArtifactLineage
    storage: ArtifactStorage
    metadata: dict[str, Any]

    @classmethod
    def from_view(
        cls, view: ExecutionArtifactView
    ) -> "ExecutionArtifactResponse":
        return cls(
            artifact_id=view.id,
            name=view.name,
            description=view.description,
            type=view.artifact_type,
            status=view.status,
            produced_by=ArtifactProducer(
                execution_id=view.execution_id,
                execution_attempt_id=view.execution_attempt_id,
                execution_step_id=view.execution_step_id,
                execution_step_attempt_id=view.execution_step_attempt_id,
            ),
            lineage=ArtifactLineage(
                parent_artifact_id=view.parent_artifact_id,
                external_parent_asset_id=view.external_parent_asset_id,
            ),
            storage=ArtifactStorage(
                type=view.storage_type,
                uri=view.uri,
                relative_path=view.relative_path,
                media_type=view.media_type,
                size_bytes=view.size_bytes,
                checksum_sha256=view.checksum_sha256,
            ),
            metadata=view.metadata,
            created_by_type=view.created_by_type,
            created_by=view.created_by,
            updated_by_type=view.updated_by_type,
            updated_by=view.updated_by,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )


class ArtifactStorageSummary(ContractModel):
    type: ArtifactStorageType
    media_type: str | None
    size_bytes: int | None


class ExecutionArtifactSummaryResponse(AuditFields):
    artifact_id: UUID
    name: str
    type: ArtifactType
    status: ArtifactStatus
    produced_by: ArtifactProducer
    storage: ArtifactStorageSummary

    @classmethod
    def from_view(
        cls, view: ExecutionArtifactView
    ) -> "ExecutionArtifactSummaryResponse":
        return cls(
            artifact_id=view.id,
            name=view.name,
            type=view.artifact_type,
            status=view.status,
            produced_by=ArtifactProducer(
                execution_id=view.execution_id,
                execution_attempt_id=view.execution_attempt_id,
                execution_step_id=view.execution_step_id,
                execution_step_attempt_id=view.execution_step_attempt_id,
            ),
            storage=ArtifactStorageSummary(
                type=view.storage_type,
                media_type=view.media_type,
                size_bytes=view.size_bytes,
            ),
            created_by_type=view.created_by_type,
            created_by=view.created_by,
            updated_by_type=view.updated_by_type,
            updated_by=view.updated_by,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )


class ExecutionArtifactPageResponse(PageResponse):
    items: list[ExecutionArtifactSummaryResponse]

    @classmethod
    def from_page(
        cls, page: Page[ExecutionArtifactView]
    ) -> "ExecutionArtifactPageResponse":
        return cls(
            items=[
                ExecutionArtifactSummaryResponse.from_view(item)
                for item in page.items
            ],
            next_cursor=page.next_cursor,
            has_more=page.next_cursor is not None,
        )


class ExecutionOperationResultResponse(ContractModel):
    operation: ExecutionOperationResponse
    steps: list[ExecutionStepResponse]

    @classmethod
    def from_bundle(
        cls, bundle: OperationResultBundle
    ) -> "ExecutionOperationResultResponse":
        return cls(
            operation=ExecutionOperationResponse.from_view(bundle.operation),
            steps=[
                ExecutionStepResponse.from_domain(
                    step, bundle.operation.execution_id
                )
                for step in bundle.steps
            ],
        )


class ExecutionAttemptResultResponse(ContractModel):
    attempt: ExecutionAttemptDetailResponse
    steps: list[ExecutionStepAttemptResponse]

    @classmethod
    def from_bundle(
        cls, bundle: AttemptResultBundle
    ) -> "ExecutionAttemptResultResponse":
        return cls(
            attempt=ExecutionAttemptDetailResponse.from_view(bundle.attempt),
            steps=[
                ExecutionStepAttemptResponse.from_view(step)
                for step in bundle.steps
            ],
        )


class ExecutionResultResponse(ContractModel):
    execution: ExecutionResponse
    operations: list[ExecutionOperationResultResponse]
    attempts: list[ExecutionAttemptResultResponse]
    artifacts: list[ExecutionArtifactResponse]

    @classmethod
    def from_bundle(
        cls, bundle: ExecutionResultBundle
    ) -> "ExecutionResultResponse":
        return cls(
            execution=ExecutionResponse.from_view(bundle.execution),
            operations=[
                ExecutionOperationResultResponse.from_bundle(operation)
                for operation in bundle.operations
            ],
            attempts=[
                ExecutionAttemptResultResponse.from_bundle(attempt)
                for attempt in bundle.attempts
            ],
            artifacts=[
                ExecutionArtifactResponse.from_view(artifact)
                for artifact in bundle.artifacts
            ],
        )
