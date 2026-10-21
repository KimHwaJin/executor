"""Contracts for SQLAlchemy model registration."""

from sqlalchemy.orm import configure_mappers

from executor_service.infrastructure.db import models  # noqa: F401
from executor_service.infrastructure.db.base import Base


def test_public_module_registers_complete_model_metadata() -> None:
    configure_mappers()

    assert set(Base.metadata.tables) == {
        "command_receipts",
        "event_retention_lease",
        "execution_artifacts",
        "execution_attempts",
        "execution_event_sequences",
        "execution_events",
        "execution_diagnostics",
        "execution_handovers",
        "execution_operations",
        "execution_retries",
        "execution_step_attempts",
        "execution_step_runs",
        "execution_steps",
        "executions",
        "executor_maintenance",
        "maintenance_run_targets",
        "maintenance_runs",
        "outbox_events",
        "runtime_target_purges",
        "runtime_targets",
    }
