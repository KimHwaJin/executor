"""Create execution and transactional outbox tables.

Revision ID: 0001
Revises:
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "executions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("cancel_idempotency_key", sa.String(length=255), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "QUEUED",
                "DISPATCHED",
                "RUNNING",
                "CANCEL_REQUESTED",
                "CANCELLED",
                "SUCCEEDED",
                "FAILED",
                name="execution_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "mode",
            sa.Enum(
                "STATIC",
                "DYNAMIC",
                name="execution_mode",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "trigger_type",
            sa.Enum(
                "INTERACTIVE",
                "BATCH",
                name="trigger_type",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "jupyter_pool",
            sa.Enum(
                "INTERACTIVE",
                "BATCH",
                name="jupyter_pool",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("kernel_name", sa.String(length=128), nullable=False),
        sa.Column(
            "code_source_type",
            sa.Enum(
                "INLINE",
                "PATH",
                name="code_source_type",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("code", sa.Text(), nullable=True),
        sa.Column("code_path", sa.Text(), nullable=True),
        sa.Column("requested_by_user_id", sa.String(length=255), nullable=False),
        sa.Column("project_id", sa.String(length=255), nullable=False),
        sa.Column("session_id", sa.String(length=255), nullable=False),
        sa.Column("execution_plan_id", sa.String(length=255), nullable=False),
        sa.Column("workflow_id", sa.String(length=255), nullable=True),
        sa.Column("correlation_id", sa.String(length=255), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("cancellation_reason", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'DISPATCHED', 'RUNNING', 'CANCEL_REQUESTED', 'CANCELLED', 'SUCCEEDED', 'FAILED')",
            name=op.f("ck_executions_valid_execution_status"),
        ),
        sa.CheckConstraint(
            "mode IN ('STATIC', 'DYNAMIC')",
            name=op.f("ck_executions_valid_execution_mode"),
        ),
        sa.CheckConstraint(
            "trigger_type IN ('INTERACTIVE', 'BATCH')",
            name=op.f("ck_executions_valid_trigger_type"),
        ),
        sa.CheckConstraint(
            "jupyter_pool IN ('INTERACTIVE', 'BATCH')",
            name=op.f("ck_executions_valid_jupyter_pool"),
        ),
        sa.CheckConstraint(
            "code_source_type IN ('INLINE', 'PATH')",
            name=op.f("ck_executions_valid_code_source_type"),
        ),
        sa.CheckConstraint(
            "(code_source_type = 'INLINE' AND code IS NOT NULL AND code_path IS NULL) OR (code_source_type = 'PATH' AND code IS NULL AND code_path IS NOT NULL)",
            name=op.f("ck_executions_valid_code_source"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_executions")),
        sa.UniqueConstraint(
            "cancel_idempotency_key", name=op.f("uq_executions_cancel_idempotency_key")
        ),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_executions_idempotency_key")),
    )
    op.create_index(
        op.f("ix_executions_correlation_id"), "executions", ["correlation_id"], unique=False
    )
    op.create_index(
        "ix_executions_status_created_at", "executions", ["status", "created_at"], unique=False
    )

    op.create_table(
        "execution_steps",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("skill_name", sa.String(length=255), nullable=True),
        sa.Column("tool_name", sa.String(length=255), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "RUNNING",
                "SUCCEEDED",
                "FAILED",
                "SKIPPED",
                "CANCELLED",
                name="step_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("input_parameters", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("sequence >= 0", name=op.f("ck_execution_steps_non_negative_sequence")),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'SKIPPED', 'CANCELLED')",
            name=op.f("ck_execution_steps_valid_step_status"),
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["executions.id"],
            name=op.f("fk_execution_steps_execution_id_executions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_execution_steps")),
        sa.UniqueConstraint(
            "execution_id", "sequence", name=op.f("uq_execution_steps_execution_sequence")
        ),
    )
    op.create_index(
        op.f("ix_execution_steps_execution_id"), "execution_steps", ["execution_id"], unique=False
    )

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("aggregate_type", sa.String(length=128), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=255), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "PUBLISHED",
                name="outbox_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.CheckConstraint(
            "status IN ('PENDING', 'PUBLISHED')",
            name=op.f("ck_outbox_events_valid_outbox_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox_events")),
    )
    op.create_index(
        op.f("ix_outbox_events_aggregate_id"), "outbox_events", ["aggregate_id"], unique=False
    )
    op.create_index(
        "ix_outbox_pending", "outbox_events", ["status", "available_at", "created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_pending", table_name="outbox_events")
    op.drop_index(op.f("ix_outbox_events_aggregate_id"), table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_index(op.f("ix_execution_steps_execution_id"), table_name="execution_steps")
    op.drop_table("execution_steps")
    op.drop_index("ix_executions_status_created_at", table_name="executions")
    op.drop_index(op.f("ix_executions_correlation_id"), table_name="executions")
    op.drop_table("executions")
