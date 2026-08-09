"""Add retained-kernel execution retry state.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "executions",
        sa.Column(
            "retryable", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.add_column(
        "executions", sa.Column("retry_from_sequence", sa.Integer(), nullable=True)
    )
    op.add_column(
        "executions",
        sa.Column("retained_kernel_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "executions",
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.alter_column("executions", "retryable", server_default=None)
    op.alter_column("executions", "retry_count", server_default=None)
    op.create_check_constraint(
        op.f("ck_executions_non_negative_retry_count"),
        "executions",
        "retry_count >= 0",
    )
    op.create_check_constraint(
        op.f("ck_executions_non_negative_retry_from_sequence"),
        "executions",
        "retry_from_sequence IS NULL OR retry_from_sequence >= 0",
    )

    op.create_table(
        "execution_retries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("from_sequence", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "from_sequence >= 0",
            name=op.f("ck_execution_retries_non_negative_from_sequence"),
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["executions.id"],
            name=op.f("fk_execution_retries_execution_id_executions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_execution_retries")),
        sa.UniqueConstraint(
            "idempotency_key", name=op.f("uq_execution_retries_idempotency_key")
        ),
    )
    op.create_index(
        op.f("ix_execution_retries_execution_id"),
        "execution_retries",
        ["execution_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_execution_retries_execution_id"), table_name="execution_retries"
    )
    op.drop_table("execution_retries")
    op.drop_constraint(
        op.f("ck_executions_non_negative_retry_from_sequence"),
        "executions",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_executions_non_negative_retry_count"),
        "executions",
        type_="check",
    )
    op.drop_column("executions", "retry_count")
    op.drop_column("executions", "retained_kernel_until")
    op.drop_column("executions", "retry_from_sequence")
    op.drop_column("executions", "retryable")
