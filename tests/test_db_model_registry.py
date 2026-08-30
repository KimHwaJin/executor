"""Contracts for SQLAlchemy model registration and compatibility imports."""

from sqlalchemy.orm import configure_mappers

from executor_service.infrastructure.db import _models as internal_models
from executor_service.infrastructure.db import models as public_models
from executor_service.infrastructure.db.base import Base


def test_public_module_reexports_internal_orm_models() -> None:
    names = (
        "CommandReceiptORM",
        "EventRetentionLeaseORM",
        "ExecutionArtifactORM",
        "ExecutionAttemptORM",
        "ExecutionEventORM",
        "ExecutionEventSequenceORM",
        "ExecutionORM",
        "ExecutionOperationORM",
        "ExecutionRetryORM",
        "ExecutionStepAttemptORM",
        "ExecutionStepORM",
        "ExecutorMaintenanceORM",
        "MaintenanceRunORM",
        "MaintenanceRunTargetORM",
        "OutboxEventORM",
        "RuntimeTargetORM",
        "RuntimeTargetPurgeORM",
    )

    for name in names:
        assert getattr(public_models, name) is getattr(internal_models, name)

    assert (
        public_models.audit_actor_constraints
        is internal_models.audit_actor_constraints
    )
    assert public_models.enum_type is internal_models.enum_type
    assert public_models.__all__ == internal_models.__all__


def test_public_module_registers_complete_model_metadata() -> None:
    configure_mappers()

    assert set(Base.metadata.tables) == {
        "command_receipts",
        "event_retention_lease",
        "execution_artifacts",
        "execution_attempts",
        "execution_event_sequences",
        "execution_events",
        "execution_operations",
        "execution_retries",
        "execution_step_attempts",
        "execution_steps",
        "executions",
        "executor_maintenance",
        "maintenance_run_targets",
        "maintenance_runs",
        "outbox_events",
        "runtime_target_purges",
        "runtime_targets",
    }
