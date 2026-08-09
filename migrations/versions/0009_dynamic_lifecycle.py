"""Add dynamic lifecycle deadlines and failure classifications.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "executions",
        sa.Column("dynamic_wait_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "executions",
        sa.Column("execution_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        op.f("ix_executions_dynamic_wait_expires_at"),
        "executions",
        ["dynamic_wait_expires_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_executions_execution_expires_at"),
        "executions",
        ["execution_expires_at"],
        unique=False,
    )
    op.drop_constraint(
        op.f("ck_executions_valid_failure_type"), "executions", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_executions_valid_failure_type"),
        "executions",
        "failure_type IS NULL OR failure_type IN ('TOOL_ERROR', 'INFRASTRUCTURE_ERROR', "
        "'WORKER_SHUTDOWN', 'JUPYTER_UNAVAILABLE', 'LEASE_EXPIRED', 'INTERNAL_ERROR', "
        "'DYNAMIC_WAIT_TIMEOUT', 'EXECUTION_TIMEOUT', 'KERNEL_LOST')",
    )
    op.drop_constraint(
        op.f("ck_execution_attempts_valid_attempt_failure_type"),
        "execution_attempts",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_execution_attempts_valid_attempt_failure_type"),
        "execution_attempts",
        "failure_type IS NULL OR failure_type IN ('TOOL_ERROR', 'INFRASTRUCTURE_ERROR', "
        "'WORKER_SHUTDOWN', 'JUPYTER_UNAVAILABLE', 'LEASE_EXPIRED', 'INTERNAL_ERROR', "
        "'DYNAMIC_WAIT_TIMEOUT', 'EXECUTION_TIMEOUT', 'KERNEL_LOST')",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE execution_attempts SET failure_type = 'INTERNAL_ERROR' "
        "WHERE failure_type IN ('DYNAMIC_WAIT_TIMEOUT', 'EXECUTION_TIMEOUT', 'KERNEL_LOST')"
    )
    op.execute(
        "UPDATE executions SET failure_type = 'INTERNAL_ERROR' "
        "WHERE failure_type IN ('DYNAMIC_WAIT_TIMEOUT', 'EXECUTION_TIMEOUT', 'KERNEL_LOST')"
    )
    op.drop_constraint(
        op.f("ck_execution_attempts_valid_attempt_failure_type"),
        "execution_attempts",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_execution_attempts_valid_attempt_failure_type"),
        "execution_attempts",
        "failure_type IS NULL OR failure_type IN ('TOOL_ERROR', 'INFRASTRUCTURE_ERROR', "
        "'WORKER_SHUTDOWN', 'JUPYTER_UNAVAILABLE', 'LEASE_EXPIRED', 'INTERNAL_ERROR')",
    )
    op.drop_constraint(
        op.f("ck_executions_valid_failure_type"), "executions", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_executions_valid_failure_type"),
        "executions",
        "failure_type IS NULL OR failure_type IN ('TOOL_ERROR', 'INFRASTRUCTURE_ERROR', "
        "'WORKER_SHUTDOWN', 'JUPYTER_UNAVAILABLE', 'LEASE_EXPIRED', 'INTERNAL_ERROR')",
    )
    op.drop_index(
        op.f("ix_executions_execution_expires_at"), table_name="executions"
    )
    op.drop_index(
        op.f("ix_executions_dynamic_wait_expires_at"), table_name="executions"
    )
    op.drop_column("executions", "execution_expires_at")
    op.drop_column("executions", "dynamic_wait_expires_at")
