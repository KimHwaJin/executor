"""Add USER/BATCH audit fields and keyset pagination indexes.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AUDITED_TABLES = (
    "executions",
    "execution_steps",
    "execution_attempts",
    "execution_step_attempts",
    "execution_artifacts",
    "outbox_events",
    "jupyter_servers",
)


def _add_actor_columns(table: str) -> None:
    op.add_column(table, sa.Column("created_by_type", sa.String(length=32), nullable=True))
    op.add_column(table, sa.Column("created_by", sa.String(length=255), nullable=True))
    op.add_column(table, sa.Column("updated_by_type", sa.String(length=32), nullable=True))
    op.add_column(table, sa.Column("updated_by", sa.String(length=255), nullable=True))
    op.create_check_constraint(
        op.f(f"ck_{table}_valid_created_by_type"),
        table,
        "created_by_type IS NULL OR created_by_type IN ('USER', 'BATCH')",
    )
    op.create_check_constraint(
        op.f(f"ck_{table}_valid_updated_by_type"),
        table,
        "updated_by_type IS NULL OR updated_by_type IN ('USER', 'BATCH')",
    )
    op.create_check_constraint(
        op.f(f"ck_{table}_complete_created_by"),
        table,
        "(created_by_type IS NULL) = (created_by IS NULL)",
    )
    op.create_check_constraint(
        op.f(f"ck_{table}_complete_updated_by"),
        table,
        "(updated_by_type IS NULL) = (updated_by IS NULL)",
    )


def _drop_actor_columns(table: str) -> None:
    op.drop_constraint(op.f(f"ck_{table}_complete_updated_by"), table, type_="check")
    op.drop_constraint(op.f(f"ck_{table}_complete_created_by"), table, type_="check")
    op.drop_constraint(op.f(f"ck_{table}_valid_updated_by_type"), table, type_="check")
    op.drop_constraint(op.f(f"ck_{table}_valid_created_by_type"), table, type_="check")
    op.drop_column(table, "updated_by")
    op.drop_column(table, "updated_by_type")
    op.drop_column(table, "created_by")
    op.drop_column(table, "created_by_type")


def upgrade() -> None:
    for table in AUDITED_TABLES:
        _add_actor_columns(table)

    op.add_column(
        "execution_attempts",
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "execution_attempts",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "execution_step_attempts",
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "execution_step_attempts",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "outbox_events",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.execute(
        "UPDATE executions SET "
        "created_by_type = CASE WHEN trigger_type = 'BATCH' THEN 'BATCH' ELSE 'USER' END, "
        "updated_by_type = CASE WHEN trigger_type = 'BATCH' THEN 'BATCH' ELSE 'USER' END, "
        "created_by = CASE WHEN trigger_type = 'BATCH' THEN 'legacy-batch:' || task_id "
        "ELSE user_id END, "
        "updated_by = CASE WHEN trigger_type = 'BATCH' THEN 'legacy-batch:' || task_id "
        "ELSE user_id END"
    )
    for table in ("execution_steps", "execution_attempts", "execution_step_attempts"):
        op.execute(
            f"UPDATE {table} AS child SET "
            "created_by_type = execution.created_by_type, "
            "created_by = execution.created_by, "
            "updated_by_type = execution.updated_by_type, "
            "updated_by = execution.updated_by "
            "FROM executions AS execution WHERE child.execution_id = execution.id"
        )
    op.execute(
        "UPDATE execution_artifacts AS child SET "
        "created_by_type = execution.created_by_type, created_by = execution.created_by, "
        "updated_by_type = execution.updated_by_type, updated_by = execution.updated_by "
        "FROM executions AS execution WHERE child.execution_id = execution.id"
    )
    op.execute(
        "UPDATE outbox_events AS event SET "
        "created_by_type = execution.created_by_type, created_by = execution.created_by, "
        "updated_by_type = execution.updated_by_type, updated_by = execution.updated_by "
        "FROM executions AS execution "
        "WHERE event.aggregate_type = 'Execution' AND event.aggregate_id = execution.id"
    )
    op.execute(
        "UPDATE execution_attempts SET created_at = started_at, "
        "updated_at = COALESCE(finished_at, heartbeat_at, started_at)"
    )
    op.execute(
        "UPDATE execution_step_attempts SET created_at = started_at, "
        "updated_at = COALESCE(finished_at, started_at)"
    )
    op.execute("UPDATE outbox_events SET updated_at = COALESCE(published_at, created_at)")

    op.alter_column("execution_attempts", "created_at", nullable=False)
    op.alter_column("execution_attempts", "updated_at", nullable=False)
    op.alter_column("execution_step_attempts", "created_at", nullable=False)
    op.alter_column("execution_step_attempts", "updated_at", nullable=False)
    op.alter_column("outbox_events", "updated_at", nullable=False)

    op.drop_index("ix_executions_status_created_at", table_name="executions")
    op.create_index(
        "ix_executions_status_created_at",
        "executions",
        ["status", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_executions_user_created_cursor",
        "executions",
        ["user_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_executions_project_created_cursor",
        "executions",
        ["project_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_executions_session_created_cursor",
        "executions",
        ["session_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_executions_task_created_cursor",
        "executions",
        ["task_id", "created_at", "id"],
        unique=False,
    )
    op.drop_index("ix_execution_artifacts_execution_created", table_name="execution_artifacts")
    op.create_index(
        "ix_execution_artifacts_execution_created",
        "execution_artifacts",
        ["execution_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_outbox_execution_cursor",
        "outbox_events",
        ["aggregate_type", "aggregate_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_jupyter_servers_created_cursor",
        "jupyter_servers",
        ["created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_jupyter_servers_created_cursor", table_name="jupyter_servers")
    op.drop_index("ix_outbox_execution_cursor", table_name="outbox_events")
    op.drop_index("ix_execution_artifacts_execution_created", table_name="execution_artifacts")
    op.create_index(
        "ix_execution_artifacts_execution_created",
        "execution_artifacts",
        ["execution_id", "created_at"],
        unique=False,
    )
    op.drop_index("ix_executions_task_created_cursor", table_name="executions")
    op.drop_index("ix_executions_session_created_cursor", table_name="executions")
    op.drop_index("ix_executions_project_created_cursor", table_name="executions")
    op.drop_index("ix_executions_user_created_cursor", table_name="executions")
    op.drop_index("ix_executions_status_created_at", table_name="executions")
    op.create_index(
        "ix_executions_status_created_at",
        "executions",
        ["status", "created_at"],
        unique=False,
    )

    op.drop_column("outbox_events", "updated_at")
    op.drop_column("execution_step_attempts", "updated_at")
    op.drop_column("execution_step_attempts", "created_at")
    op.drop_column("execution_attempts", "updated_at")
    op.drop_column("execution_attempts", "created_at")

    for table in reversed(AUDITED_TABLES):
        _drop_actor_columns(table)
