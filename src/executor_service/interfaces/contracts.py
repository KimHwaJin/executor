"""Transport-shared Pydantic contracts for REST responses and MCP structured content."""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from executor_service.application.commands import (
    MaterializeArtifactCommand,
)
from executor_service.application.execution_queries import (
    ExecutionArtifactView,
    ExecutionAttemptView,
    ExecutionDetailView,
    ExecutionEventView,
    ExecutionOperationView,
    ExecutionStepAttemptView,
)
from executor_service.application.execution_results import (
    ExecutionResultBundle,
    OperationResultBundle,
)
from executor_service.application.pagination import Page
from executor_service.domain.enums import (
    ArtifactStatus,
    ArtifactStorageType,
    ArtifactType,
    AttemptStatus,
    CodeSourceType,
    OperationStatus,
    OutboxStatus,
    RetryStrategy,
    RuntimeAbortStatus,
    RuntimeSessionCleanupStatus,
    RuntimeType,
    StepStatus,
)
from executor_service.domain.models import (
    ExecutionStep,
)
from executor_service.interfaces._contracts.common import (
    ActorInput,
    AuditFields,
    ContractModel,
    Lifecycle,
    PageResponse,
)
from executor_service.interfaces._contracts.execution_inputs import (  # noqa: F401
    ExecutionContext,
    ExecutionLifecycleInput,
    ExecutionOperationInput,
    ExecutionRuntimeInput,
    ExecutionSubmitRequest,
    ExecutionTriggerInput,
)
from executor_service.interfaces._contracts.executions import (  # noqa: F401
    DeadlinesResponse,
    ExecutionCommandResponse,
    ExecutionCommandState,
    ExecutionLifecycleResponse,
    ExecutionOperationReceipt,
    ExecutionPageResponse,
    ExecutionResponse,
    ExecutionRuntime,
    ExecutionState,
    ExecutionStepReceipt,
    ExecutionSummaryResponse,
    FailureResponse,
    NotebookProjectionResponse,
    RecoveryResponse,
    RetryResponse,
    WorkspaceResponse,
)
from executor_service.interfaces._contracts.maintenance import (  # noqa: F401
    ExecutorMaintenanceResponse,
    MaintenanceRunResponse,
    MaintenanceRunTargetPageResponse,
)
from executor_service.interfaces._contracts.notebooks import (  # noqa: F401
    ExecutionNotebookCellResponse,
    ExecutionNotebookResponse,
)
from executor_service.interfaces._contracts.runtimes import (  # noqa: F401
    RuntimePoolPageResponse,
    RuntimePoolResponse,
    RuntimeTargetPageResponse,
    RuntimeTargetResponse,
    RuntimeTargetUpsertRequest,
)
from executor_service.result_summaries import OutputSummary


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
    size_bytes: int
    complete: bool
    representation_count: int
    total_size_bytes: int


class StepResultSummary(ContractModel):
    status: StepStatus
    output_summary: OutputSummary
    error_message: str | None


class StepResult(StepResultSummary):
    result_ref: StepResultReference | None


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
                        size_bytes=step.result_manifest_size_bytes,
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
                        and step.result_manifest_size_bytes is not None
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
                        size_bytes=view.result_manifest_size_bytes,
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
                        and view.result_manifest_size_bytes is not None
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
    result: StepResult
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
                        size_bytes=view.result_manifest_size_bytes,
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
                        and view.result_manifest_size_bytes is not None
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
    items: list["ExecutionOperationSummaryResponse"]

    @classmethod
    def from_page(
        cls, page: Page[ExecutionOperationView]
    ) -> "ExecutionOperationPageResponse":
        return cls(
            items=[
                ExecutionOperationSummaryResponse.from_view(item)
                for item in page.items
            ],
            next_cursor=page.next_cursor,
            has_more=page.next_cursor is not None,
        )


class ExecutionOperationSummaryResponse(AuditFields):
    operation_id: UUID
    operation_number: int
    sequence_range: OperationSequenceRange
    result: OperationResult
    lifecycle: Lifecycle
    step_count: int

    @classmethod
    def from_view(
        cls, view: ExecutionOperationView
    ) -> "ExecutionOperationSummaryResponse":
        return cls(
            operation_id=view.id,
            operation_number=view.operation_number,
            sequence_range=OperationSequenceRange(
                first=view.first_sequence, last=view.last_sequence
            ),
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
    execution_id: UUID
    event_sequence: int
    schema_version: str
    event_type: str
    payload: dict[str, Any]
    occurred_at: datetime
    delivery: EventDelivery | None

    @classmethod
    def from_view(cls, view: ExecutionEventView) -> "ExecutionEventResponse":
        return cls(
            event_id=view.id,
            execution_id=view.execution_id,
            event_sequence=view.event_sequence,
            schema_version=view.schema_version,
            event_type=view.event_type,
            payload=view.payload,
            occurred_at=view.created_at,
            delivery=(
                EventDelivery(
                    status=view.delivery_status,
                    attempt_count=view.publish_attempt_count,
                    available_at=view.available_at,
                    published_at=view.published_at,
                    last_error=view.last_error,
                )
                if view.delivery_status is not None
                and view.publish_attempt_count is not None
                and view.available_at is not None
                else None
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


class ExecutionResultExecutionResponse(ContractModel):
    execution_id: UUID
    state: ExecutionCommandState

    @classmethod
    def from_view(
        cls, view: ExecutionDetailView
    ) -> "ExecutionResultExecutionResponse":
        return cls(
            execution_id=view.id,
            state=ExecutionCommandState(
                status=view.status,
                version=view.version,
            ),
        )


class ExecutionResultStepResponse(ContractModel):
    step_id: UUID
    sequence: int
    lineage: ToolReference
    result: StepResult
    lifecycle: Lifecycle

    @classmethod
    def from_domain(
        cls, step: ExecutionStep, execution_id: UUID
    ) -> "ExecutionResultStepResponse":
        detail = ExecutionStepResponse.from_domain(step, execution_id)
        return cls(
            step_id=detail.step_id,
            sequence=detail.sequence,
            lineage=detail.lineage,
            result=detail.result,
            lifecycle=detail.lifecycle,
        )


class ExecutionResultOperationResponse(ContractModel):
    operation_id: UUID
    operation_number: int
    sequence_range: OperationSequenceRange
    result: OperationResult
    lifecycle: Lifecycle
    steps: list[ExecutionResultStepResponse]

    @classmethod
    def from_bundle(
        cls, bundle: OperationResultBundle
    ) -> "ExecutionResultOperationResponse":
        operation = bundle.operation
        return cls(
            operation_id=operation.id,
            operation_number=operation.operation_number,
            sequence_range=OperationSequenceRange(
                first=operation.first_sequence,
                last=operation.last_sequence,
            ),
            result=OperationResult(
                status=operation.status,
                error_message=operation.error_message,
            ),
            lifecycle=Lifecycle(
                started_at=operation.started_at,
                finished_at=operation.finished_at,
            ),
            steps=[
                ExecutionResultStepResponse.from_domain(
                    step, operation.execution_id
                )
                for step in bundle.steps
            ],
        )


class ExecutionOperationResultResponse(ContractModel):
    execution: ExecutionResultExecutionResponse
    operation: ExecutionResultOperationResponse

    @classmethod
    def from_bundle(
        cls, bundle: OperationResultBundle
    ) -> "ExecutionOperationResultResponse":
        return cls(
            execution=ExecutionResultExecutionResponse.from_view(
                bundle.execution
            ),
            operation=ExecutionResultOperationResponse.from_bundle(bundle),
        )


class ExecutionResultResponse(ContractModel):
    execution: ExecutionResultExecutionResponse
    operations: list[ExecutionResultOperationResponse]
    attempts: list[ExecutionAttemptResponse]
    artifacts: list[ExecutionArtifactSummaryResponse]

    @classmethod
    def from_bundle(
        cls, bundle: ExecutionResultBundle
    ) -> "ExecutionResultResponse":
        return cls(
            execution=ExecutionResultExecutionResponse.from_view(
                bundle.execution
            ),
            operations=[
                ExecutionResultOperationResponse.from_bundle(operation)
                for operation in bundle.operations
            ],
            attempts=[
                ExecutionAttemptResponse.from_view(attempt)
                for attempt in bundle.attempts
            ],
            artifacts=[
                ExecutionArtifactSummaryResponse.from_view(artifact)
                for artifact in bundle.artifacts
            ],
        )
