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
from executor_service.interfaces.contracts.common import (
    AuditFields,
    ContractModel,
    Lifecycle,
    PageResponse,
    StatusReasonResponse,
)


class ExecutionContext(ContractModel):
    user_id: str
    project_id: str | None
    session_id: str | None


class ExecutionTrigger(ContractModel):
    type: TriggerType


class ExecutionRuntime(ContractModel):
    type: RuntimeType
    pool: RuntimePool
    profile: str
    target_id: UUID | None
    # The Jupyter kernel running this Execution, not the caller's session.
    kernel_id: str | None


class ExecutionState(ContractModel):
    status: ExecutionStatus
    version: int
    # Null only while RUNNING under a live Worker.
    reason: StatusReasonResponse | None


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
            created_by_client=execution.created_by_client,
            created_by=execution.created_by,
            updated_by_client=execution.updated_by_client,
            updated_by=execution.updated_by,
            created_at=execution.created_at,
            updated_at=execution.updated_at,
        )


class ExecutionResponse(AuditFields):
    execution_id: UUID
    trigger: ExecutionTrigger
    tenant_id: str
    context: ExecutionContext
    metadata: dict[str, Any]
    runtime: ExecutionRuntime
    state: ExecutionState
    workspace: WorkspaceResponse
    retry: RetryResponse
    recovery: RecoveryResponse
    deadlines: DeadlinesResponse
    lifecycle: ExecutionLifecycleResponse

    @classmethod
    def from_view(cls, execution: ExecutionDetailView) -> "ExecutionResponse":
        return cls(
            execution_id=execution.id,
            lifecycle=ExecutionLifecycleResponse(
                operation_mode=execution.operation_mode,
                operation_wait_timeout_seconds=(
                    execution.operation_wait_timeout_seconds
                ),
                started_at=execution.started_at,
                finished_at=execution.finished_at,
            ),
            trigger=ExecutionTrigger(type=execution.trigger_type),
            context=ExecutionContext(
                user_id=execution.user_id,
                project_id=execution.project_id,
                session_id=execution.session_id,
            ),
            tenant_id=execution.tenant_id,
            metadata=dict(execution.metadata),
            runtime=ExecutionRuntime(
                type=execution.runtime_type,
                pool=execution.runtime_pool,
                profile=execution.runtime_profile,
                target_id=execution.runtime_target_id,
                kernel_id=execution.runtime_session_id,
            ),
            state=ExecutionState(
                status=execution.status,
                version=execution.version,
                reason=StatusReasonResponse.from_view(execution.reason),
            ),
            retry=RetryResponse(
                strategy=execution.retry_strategy,
                count=execution.retry_count,
                from_sequence=execution.retry_from_sequence,
                retained_runtime_session_until=(
                    execution.retained_runtime_session_until
                ),
            ),
            created_by_client=execution.created_by_client,
            created_by=execution.created_by,
            updated_by_client=execution.updated_by_client,
            updated_by=execution.updated_by,
            created_at=execution.created_at,
            updated_at=execution.updated_at,
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
    trigger: ExecutionTrigger
    tenant_id: str
    context: ExecutionContext
    metadata: dict[str, Any]
    state: ExecutionState
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
            trigger=ExecutionTrigger(type=execution.trigger_type),
            context=ExecutionContext(
                user_id=execution.user_id,
                project_id=execution.project_id,
                session_id=execution.session_id,
            ),
            tenant_id=execution.tenant_id,
            metadata=dict(execution.metadata),
            state=ExecutionState(
                status=execution.status,
                version=execution.version,
                reason=StatusReasonResponse.from_view(execution.reason),
            ),
            lifecycle=Lifecycle(
                started_at=execution.started_at,
                finished_at=execution.finished_at,
            ),
            step_count=execution.step_count,
            created_by_client=execution.created_by_client,
            created_by=execution.created_by,
            updated_by_client=execution.updated_by_client,
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
