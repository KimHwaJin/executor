"""Shared constants for durable Executor Maintenance Runs."""

from executor_service.domain.enums import (
    ExecutionStatus,
    MaintenanceRunStatus,
    RuntimeSessionCleanupStatus,
)

RUN_CURSOR_KIND = "maintenance_run_targets"
PROCESS_BATCH_SIZE = 50
RUNNABLE_STATUSES = (
    MaintenanceRunStatus.REQUESTED,
    MaintenanceRunStatus.RUNNING,
)
ACTIVE_EXECUTION_STATUSES = (
    ExecutionStatus.DISPATCHED,
    ExecutionStatus.RUNNING,
    ExecutionStatus.WAITING_FOR_OPERATION,
    ExecutionStatus.FINALIZING,
    ExecutionStatus.CANCEL_REQUESTED,
)
UNRESOLVED_CLEANUP_STATUSES = (
    RuntimeSessionCleanupStatus.PENDING,
    RuntimeSessionCleanupStatus.FAILED,
)
