"""Executor maintenance and maintenance-run transport contracts."""

from datetime import datetime
from uuid import UUID

from executor_service.application.maintenance import ExecutorMaintenanceView
from executor_service.application.maintenance_runs import (
    MaintenanceRunTargetView,
    MaintenanceRunView,
)
from executor_service.application.pagination import Page
from executor_service.domain.enums import (
    ExecutionStatus,
    ExecutorAdmissionState,
    MaintenanceRunAction,
    MaintenanceRunStatus,
    MaintenanceRunTargetStatus,
)
from executor_service.interfaces._contracts.common import (
    AuditFields,
    ContractModel,
)


class ExecutorAdmissionResponse(ContractModel):
    state: ExecutorAdmissionState
    accepting_new_executions: bool
    version: int


class ExecutorWorkloadResponse(ContractModel):
    queued_execution_count: int
    active_execution_count: int
    cancel_requested_count: int


class ExecutorCleanupResponse(ContractModel):
    unresolved_cleanup_count: int
    active_runtime_session_count: int


class ActiveMaintenanceRunResponse(ContractModel):
    maintenance_run_id: UUID
    action: MaintenanceRunAction
    status: MaintenanceRunStatus


class ExecutorMaintenanceResponse(AuditFields):
    admission: ExecutorAdmissionResponse
    workload: ExecutorWorkloadResponse
    cleanup: ExecutorCleanupResponse
    active_run: ActiveMaintenanceRunResponse | None
    safe_to_shutdown: bool

    @classmethod
    def from_view(
        cls, view: ExecutorMaintenanceView
    ) -> "ExecutorMaintenanceResponse":
        return cls(
            admission=ExecutorAdmissionResponse(
                state=view.admission_state,
                accepting_new_executions=view.accepting_new_executions,
                version=view.version,
            ),
            workload=ExecutorWorkloadResponse(
                queued_execution_count=view.queued_execution_count,
                active_execution_count=view.active_execution_count,
                cancel_requested_count=view.cancel_requested_count,
            ),
            cleanup=ExecutorCleanupResponse(
                unresolved_cleanup_count=view.unresolved_cleanup_count,
                active_runtime_session_count=(
                    view.active_runtime_session_count
                ),
            ),
            active_run=(
                ActiveMaintenanceRunResponse(
                    maintenance_run_id=view.active_run_id,
                    action=view.active_run_action,
                    status=view.active_run_status,
                )
                if view.active_run_id is not None
                and view.active_run_action is not None
                and view.active_run_status is not None
                else None
            ),
            safe_to_shutdown=view.safe_to_shutdown,
            created_by_type=view.created_by_type,
            created_by=view.created_by,
            updated_by_type=view.updated_by_type,
            updated_by=view.updated_by,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )


class MaintenanceRunTargetCountsResponse(ContractModel):
    total: int
    pending: int
    stop_requested: int
    stopped: int
    failed: int
    remaining: int


class MaintenanceRunResponse(AuditFields):
    maintenance_run_id: UUID
    action: MaintenanceRunAction
    status: MaintenanceRunStatus
    targets: MaintenanceRunTargetCountsResponse
    error_message: str | None
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def from_view(cls, view: MaintenanceRunView) -> "MaintenanceRunResponse":
        return cls(
            maintenance_run_id=view.id,
            action=view.action,
            status=view.status,
            targets=MaintenanceRunTargetCountsResponse(
                total=view.counts.total,
                pending=view.counts.pending,
                stop_requested=view.counts.stop_requested,
                stopped=view.counts.stopped,
                failed=view.counts.failed,
                remaining=view.counts.remaining,
            ),
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


class MaintenanceRunTargetResponse(AuditFields):
    target_id: UUID
    maintenance_run_id: UUID
    execution_id: UUID
    selected_execution_status: ExecutionStatus
    status: MaintenanceRunTargetStatus
    error_message: str | None
    stop_requested_at: datetime | None
    completed_at: datetime | None

    @classmethod
    def from_view(
        cls, view: MaintenanceRunTargetView
    ) -> "MaintenanceRunTargetResponse":
        return cls(
            target_id=view.id,
            maintenance_run_id=view.maintenance_run_id,
            execution_id=view.execution_id,
            selected_execution_status=view.selected_execution_status,
            status=view.status,
            error_message=view.error_message,
            stop_requested_at=view.stop_requested_at,
            completed_at=view.completed_at,
            created_by_type=view.created_by_type,
            created_by=view.created_by,
            updated_by_type=view.updated_by_type,
            updated_by=view.updated_by,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )


class MaintenanceRunTargetPageResponse(ContractModel):
    items: list[MaintenanceRunTargetResponse]
    next_cursor: str | None
    has_more: bool

    @classmethod
    def from_page(
        cls, page: Page[MaintenanceRunTargetView]
    ) -> "MaintenanceRunTargetPageResponse":
        return cls(
            items=[
                MaintenanceRunTargetResponse.from_view(item)
                for item in page.items
            ],
            next_cursor=page.next_cursor,
            has_more=page.next_cursor is not None,
        )
