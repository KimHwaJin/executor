"""Transport-shared Pydantic contracts for REST responses and MCP structured content."""

from uuid import UUID

from executor_service.application.execution_queries import (
    ExecutionDetailView,
)
from executor_service.application.execution_results import (
    ExecutionResultBundle,
    OperationResultBundle,
)
from executor_service.domain.models import (
    ExecutionStep,
)
from executor_service.interfaces._contracts.artifacts import (  # noqa: F401
    ArtifactLineage,
    ArtifactProducer,
    ArtifactSource,
    ArtifactStorage,
    ArtifactStorageSummary,
    ExecutionArtifactMaterializeRequest,
    ExecutionArtifactPageResponse,
    ExecutionArtifactResponse,
    ExecutionArtifactSummaryResponse,
    InlineArtifactSource,
    PathArtifactSource,
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
from executor_service.interfaces._contracts.common import (  # noqa: F401
    ActorInput,
    AuditFields,
    ContractModel,
    Lifecycle,
    PageResponse,
)
from executor_service.interfaces._contracts.events import (  # noqa: F401
    EventDelivery,
    ExecutionEventPageResponse,
    ExecutionEventResponse,
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
