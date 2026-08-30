"""Core Execution command, detail, and summary response contracts."""

from datetime import datetime
from typing import Any
from uuid import UUID

from executor_service.application.execution_queries import (
    ExecutionDetailView,
    ExecutionSummaryView,
)
from executor_service.application.pagination import Page
from executor_service.domain.enums import (
    ExecutionStatus,
    FailureType,
    OperationMode,
    RetryStrategy,
    RuntimeAbortStatus,
    RuntimePool,
    RuntimeSessionCleanupStatus,
    RuntimeType,
    TriggerType,
)
from executor_service.domain.models import (
    Execution,
    NotebookProjectionStatus,
)
from executor_service.interfaces._contracts.common import (
    AuditFields,
    ContractModel,
    Lifecycle,
    PageResponse,
)
from executor_service.interfaces._contracts.execution_inputs import (
    ExecutionContext,
)


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
            type=execution.failure_type,
            message=execution.error_message,
        )
    return {
        "execution_id": execution.id,
        "lifecycle": ExecutionLifecycleResponse(
            operation_mode=execution.operation_mode,
            operation_wait_timeout_seconds=(
                execution.operation_wait_timeout_seconds
            ),
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
            retained_runtime_session_until=(
                execution.retained_runtime_session_until
            ),
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
    operation: ExecutionOperationReceipt | None
    state: ExecutionCommandState

    @classmethod
    def from_domain(
        cls,
        execution: Execution,
        *,
        operation_id: UUID | None = None,
    ) -> "ExecutionCommandResponse":
        return cls(
            execution_id=execution.id,
            operation=(
                ExecutionOperationReceipt(
                    operation_id=operation_id,
                    steps=[
                        ExecutionStepReceipt(
                            sequence=step.sequence,
                            step_id=step.id,
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
        cls,
        execution: ExecutionDetailView | Execution,
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
                runtime_session_cleanup_status=(
                    execution.runtime_session_cleanup_status
                ),
                runtime_abort_status=execution.runtime_abort_status,
            ),
            deadlines=DeadlinesResponse(
                operation_wait_expires_at=(
                    execution.operation_wait_expires_at
                ),
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
        cls,
        execution: ExecutionSummaryView,
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
        cls,
        page: Page[ExecutionSummaryView],
    ) -> "ExecutionPageResponse":
        return cls(
            items=[
                ExecutionSummaryResponse.from_view(item) for item in page.items
            ],
            next_cursor=page.next_cursor,
            has_more=page.next_cursor is not None,
        )
