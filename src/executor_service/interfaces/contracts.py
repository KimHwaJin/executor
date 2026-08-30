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
    ExecutionDetailView,
    ExecutionEventView,
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
    CodeSourceType,
    OutboxStatus,
)
from executor_service.domain.models import (
    ExecutionStep,
)
from executor_service.interfaces._contracts.attempts import (  # noqa: F401
    AttemptLease,
    AttemptRecovery,
    AttemptRuntime,
    AttemptState,
    ExecutionAttemptDetailResponse,
    ExecutionAttemptPageResponse,
    ExecutionAttemptResponse,
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
from executor_service.interfaces._contracts.operations import (  # noqa: F401
    ExecutionOperationPageResponse,
    ExecutionOperationResponse,
    ExecutionOperationSummaryResponse,
    OperationResult,
    OperationSequenceRange,
)
from executor_service.interfaces._contracts.runtimes import (  # noqa: F401
    RuntimePoolPageResponse,
    RuntimePoolResponse,
    RuntimeTargetPageResponse,
    RuntimeTargetResponse,
    RuntimeTargetUpsertRequest,
)
from executor_service.interfaces._contracts.steps import (  # noqa: F401
    ExecutionStepAttemptPageResponse,
    ExecutionStepAttemptResponse,
    ExecutionStepAttemptSummaryResponse,
    ExecutionStepPageResponse,
    ExecutionStepResponse,
    ExecutionStepSummaryResponse,
    StepResult,
    StepResultReference,
    StepResultSummary,
    StepSourceResponse,
    ToolReference,
)


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
