"""Execution Attempt transport contracts."""

from datetime import datetime
from uuid import UUID

from executor_service.application.execution_queries import ExecutionAttemptView
from executor_service.application.pagination import Page
from executor_service.domain.enums import (
    AttemptStatus,
    RetryStrategy,
    RuntimeAbortStatus,
    RuntimeSessionCleanupStatus,
    RuntimeType,
)
from executor_service.interfaces._contracts.common import (
    AuditFields,
    ContractModel,
    Lifecycle,
    PageResponse,
)
from executor_service.interfaces._contracts.executions import FailureResponse


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
        cls,
        view: ExecutionAttemptView,
    ) -> "ExecutionAttemptResponse":
        failure = None
        if view.failure_type is not None and view.error_message is not None:
            failure = FailureResponse(
                type=view.failure_type,
                message=view.error_message,
            )
        return cls(
            attempt_id=view.id,
            execution_id=view.execution_id,
            attempt_number=view.attempt_number,
            state=AttemptState(status=view.status),
            failure=failure,
            lifecycle=Lifecycle(
                started_at=view.started_at,
                finished_at=view.finished_at,
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
        cls,
        view: ExecutionAttemptView,
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
                runtime_session_cleanup_status=(
                    view.runtime_session_cleanup_status
                ),
                runtime_abort_status=view.runtime_abort_status,
            ),
        )


class ExecutionAttemptPageResponse(PageResponse):
    items: list[ExecutionAttemptResponse]

    @classmethod
    def from_page(
        cls,
        page: Page[ExecutionAttemptView],
    ) -> "ExecutionAttemptPageResponse":
        return cls(
            items=[
                ExecutionAttemptResponse.from_view(item) for item in page.items
            ],
            next_cursor=page.next_cursor,
            has_more=page.next_cursor is not None,
        )
