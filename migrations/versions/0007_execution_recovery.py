"""Add classified execution failure and recovery state.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FAILURE_TYPE = sa.Enum(
    "TOOL_ERROR",
    "INFRASTRUCTURE_ERROR",
    "WORKER_SHUTDOWN",
    "JUPYTER_UNAVAILABLE",
    "LEASE_EXPIRED",
    "INTERNAL_ERROR",
    name="failure_type",
    native_enum=False,
    create_constraint=False,
    length=32,
)
ATTEMPT_FAILURE_TYPE = sa.Enum(
    "TOOL_ERROR",
    "INFRASTRUCTURE_ERROR",
    "WORKER_SHUTDOWN",
    "JUPYTER_UNAVAILABLE",
    "LEASE_EXPIRED",
    "INTERNAL_ERROR",
    name="attempt_failure_type",
    native_enum=False,
    create_constraint=False,
    length=32,
)
RETRY_STRATEGY = sa.Enum(
    "NOT_RETRYABLE",
    "FROM_FAILED_STEP",
    "FROM_START",
    name="retry_strategy",
    native_enum=False,
    create_constraint=False,
    length=32,
)
ATTEMPT_RETRY_STRATEGY = sa.Enum(
    "NOT_RETRYABLE",
    "FROM_FAILED_STEP",
    "FROM_START",
    name="attempt_retry_strategy",
    native_enum=False,
    create_constraint=False,
    length=32,
)
KERNEL_CLEANUP_STATUS = sa.Enum(
    "NOT_REQUIRED",
    "PENDING",
    "SUCCEEDED",
    "FAILED",
    name="kernel_cleanup_status",
    native_enum=False,
    create_constraint=False,
    length=32,
)
ATTEMPT_KERNEL_CLEANUP_STATUS = sa.Enum(
    "NOT_REQUIRED",
    "PENDING",
    "SUCCEEDED",
    "FAILED",
    name="attempt_kernel_cleanup_status",
    native_enum=False,
    create_constraint=False,
    length=32,
)


def upgrade() -> None:
    op.add_column("executions", sa.Column("failure_type", FAILURE_TYPE, nullable=True))
    op.add_column(
        "executions",
        sa.Column(
            "retry_strategy",
            RETRY_STRATEGY,
            nullable=False,
            server_default="NOT_RETRYABLE",
        ),
    )
    op.add_column(
        "executions",
        sa.Column("recovery_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "executions",
        sa.Column(
            "kernel_cleanup_status",
            KERNEL_CLEANUP_STATUS,
            nullable=False,
            server_default="NOT_REQUIRED",
        ),
    )
    op.execute(
        "UPDATE executions SET retry_strategy = 'FROM_FAILED_STEP' WHERE retryable = true"
    )
    op.alter_column("executions", "retry_strategy", server_default=None)
    op.alter_column("executions", "recovery_count", server_default=None)
    op.alter_column("executions", "kernel_cleanup_status", server_default=None)
    op.create_check_constraint(
        op.f("ck_executions_non_negative_recovery_count"),
        "executions",
        "recovery_count >= 0",
    )
    op.create_check_constraint(
        op.f("ck_executions_valid_failure_type"),
        "executions",
        "failure_type IS NULL OR failure_type IN ('TOOL_ERROR', 'INFRASTRUCTURE_ERROR', "
        "'WORKER_SHUTDOWN', 'JUPYTER_UNAVAILABLE', 'LEASE_EXPIRED', 'INTERNAL_ERROR')",
    )
    op.create_check_constraint(
        op.f("ck_executions_valid_retry_strategy"),
        "executions",
        "retry_strategy IN ('NOT_RETRYABLE', 'FROM_FAILED_STEP', 'FROM_START')",
    )
    op.create_check_constraint(
        op.f("ck_executions_valid_kernel_cleanup_status"),
        "executions",
        "kernel_cleanup_status IN ('NOT_REQUIRED', 'PENDING', 'SUCCEEDED', 'FAILED')",
    )

    op.add_column(
        "execution_attempts",
        sa.Column("failure_type", ATTEMPT_FAILURE_TYPE, nullable=True),
    )
    op.add_column(
        "execution_attempts",
        sa.Column(
            "retry_strategy",
            ATTEMPT_RETRY_STRATEGY,
            nullable=False,
            server_default="NOT_RETRYABLE",
        ),
    )
    op.add_column(
        "execution_attempts",
        sa.Column(
            "kernel_cleanup_status",
            ATTEMPT_KERNEL_CLEANUP_STATUS,
            nullable=False,
            server_default="NOT_REQUIRED",
        ),
    )
    op.alter_column("execution_attempts", "retry_strategy", server_default=None)
    op.alter_column("execution_attempts", "kernel_cleanup_status", server_default=None)
    op.create_check_constraint(
        op.f("ck_execution_attempts_valid_attempt_failure_type"),
        "execution_attempts",
        "failure_type IS NULL OR failure_type IN ('TOOL_ERROR', 'INFRASTRUCTURE_ERROR', "
        "'WORKER_SHUTDOWN', 'JUPYTER_UNAVAILABLE', 'LEASE_EXPIRED', 'INTERNAL_ERROR')",
    )
    op.create_check_constraint(
        op.f("ck_execution_attempts_valid_attempt_retry_strategy"),
        "execution_attempts",
        "retry_strategy IN ('NOT_RETRYABLE', 'FROM_FAILED_STEP', 'FROM_START')",
    )
    op.create_check_constraint(
        op.f("ck_execution_attempts_valid_attempt_kernel_cleanup_status"),
        "execution_attempts",
        "kernel_cleanup_status IN ('NOT_REQUIRED', 'PENDING', 'SUCCEEDED', 'FAILED')",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_execution_attempts_valid_attempt_kernel_cleanup_status"),
        "execution_attempts",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_execution_attempts_valid_attempt_retry_strategy"),
        "execution_attempts",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_execution_attempts_valid_attempt_failure_type"),
        "execution_attempts",
        type_="check",
    )
    op.drop_column("execution_attempts", "kernel_cleanup_status")
    op.drop_column("execution_attempts", "retry_strategy")
    op.drop_column("execution_attempts", "failure_type")

    op.drop_constraint(
        op.f("ck_executions_valid_kernel_cleanup_status"),
        "executions",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_executions_valid_retry_strategy"), "executions", type_="check"
    )
    op.drop_constraint(
        op.f("ck_executions_valid_failure_type"), "executions", type_="check"
    )
    op.drop_constraint(
        op.f("ck_executions_non_negative_recovery_count"),
        "executions",
        type_="check",
    )
    op.drop_column("executions", "kernel_cleanup_status")
    op.drop_column("executions", "recovery_count")
    op.drop_column("executions", "retry_strategy")
    op.drop_column("executions", "failure_type")
