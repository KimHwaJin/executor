"""Contracts for SQLAlchemy model registration and compatibility imports."""

from sqlalchemy.orm import configure_mappers

from executor_service.infrastructure.db import _models as auxiliary_models
from executor_service.infrastructure.db import models as public_models
from executor_service.infrastructure.db.base import Base


def test_public_module_reexports_auxiliary_orm_models() -> None:
    names = (
        "CommandReceiptORM",
        "EventRetentionLeaseORM",
        "ExecutorMaintenanceORM",
        "MaintenanceRunORM",
        "MaintenanceRunTargetORM",
        "RuntimeTargetORM",
        "RuntimeTargetPurgeORM",
    )

    for name in names:
        assert getattr(public_models, name) is getattr(auxiliary_models, name)


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
