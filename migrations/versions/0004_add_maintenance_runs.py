"""Add durable and recoverable Maintenance Runs.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "maintenance_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("lease_owner", sa.String(length=255), nullable=True),
        sa.Column(
            "lease_expires_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_by_type", sa.String(length=32), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("updated_by_type", sa.String(length=32), nullable=True),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "action IN ('STOP_ACTIVE_EXECUTIONS')",
            name=op.f("ck_maintenance_runs_valid_action"),
        ),
        sa.CheckConstraint(
            "status IN ('REQUESTED', 'RUNNING', 'SUCCEEDED', 'FAILED')",
            name=op.f("ck_maintenance_runs_valid_status"),
        ),
        sa.CheckConstraint(
            "fencing_token >= 0",
            name=op.f("ck_maintenance_runs_non_negative_fencing_token"),
        ),
        sa.CheckConstraint(
            "created_by_type IS NULL OR created_by_type IN "
            "('AGENT', 'USER', 'BATCH')",
            name=op.f("ck_maintenance_runs_valid_created_by_type"),
        ),
        sa.CheckConstraint(
            "updated_by_type IS NULL OR updated_by_type IN "
            "('AGENT', 'USER', 'BATCH')",
            name=op.f("ck_maintenance_runs_valid_updated_by_type"),
        ),
        sa.CheckConstraint(
            "(created_by_type IS NULL) = (created_by IS NULL)",
            name=op.f("ck_maintenance_runs_complete_created_by"),
        ),
        sa.CheckConstraint(
            "(updated_by_type IS NULL) = (updated_by IS NULL)",
            name=op.f("ck_maintenance_runs_complete_updated_by"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_maintenance_runs")),
        sa.UniqueConstraint(
            "idempotency_key",
            name=op.f("uq_maintenance_runs_idempotency_key"),
        ),
    )
    op.create_index(
        "ix_maintenance_runs_recovery",
        "maintenance_runs",
        ["status", "lease_expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_maintenance_runs_created",
        "maintenance_runs",
        ["created_at", "id"],
        unique=False,
    )
    op.create_table(
        "maintenance_run_targets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("maintenance_run_id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column(
            "selected_execution_status", sa.String(length=32), nullable=False
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "stop_requested_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_type", sa.String(length=32), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("updated_by_type", sa.String(length=32), nullable=True),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING', 'STOP_REQUESTED', 'STOPPED', 'FAILED')",
            name=op.f("ck_maintenance_run_targets_valid_status"),
        ),
        sa.CheckConstraint(
            "created_by_type IS NULL OR created_by_type IN "
            "('AGENT', 'USER', 'BATCH')",
            name=op.f("ck_maintenance_run_targets_valid_created_by_type"),
        ),
        sa.CheckConstraint(
            "updated_by_type IS NULL OR updated_by_type IN "
            "('AGENT', 'USER', 'BATCH')",
            name=op.f("ck_maintenance_run_targets_valid_updated_by_type"),
        ),
        sa.CheckConstraint(
            "(created_by_type IS NULL) = (created_by IS NULL)",
            name=op.f("ck_maintenance_run_targets_complete_created_by"),
        ),
        sa.CheckConstraint(
            "(updated_by_type IS NULL) = (updated_by IS NULL)",
            name=op.f("ck_maintenance_run_targets_complete_updated_by"),
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["executions.id"],
            name=op.f("fk_maintenance_run_targets_execution_id_executions"),
        ),
        sa.ForeignKeyConstraint(
            ["maintenance_run_id"],
            ["maintenance_runs.id"],
            name=op.f(
                "fk_maintenance_run_targets_maintenance_run_id_maintenance_runs"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_maintenance_run_targets")),
        sa.UniqueConstraint(
            "maintenance_run_id",
            "execution_id",
            name="uq_maintenance_run_targets_run_execution",
        ),
    )
    op.create_index(
        "ix_maintenance_run_targets_run_status",
        "maintenance_run_targets",
        ["maintenance_run_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_maintenance_run_targets_cursor",
        "maintenance_run_targets",
        ["maintenance_run_id", "created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_maintenance_run_targets_cursor",
        table_name="maintenance_run_targets",
    )
    op.drop_index(
        "ix_maintenance_run_targets_run_status",
        table_name="maintenance_run_targets",
    )
    op.drop_table("maintenance_run_targets")
    op.drop_index("ix_maintenance_runs_created", table_name="maintenance_runs")
    op.drop_index(
        "ix_maintenance_runs_recovery", table_name="maintenance_runs"
    )
    op.drop_table("maintenance_runs")
