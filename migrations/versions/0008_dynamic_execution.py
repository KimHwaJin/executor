"""Add append-only dynamic execution cells and waiting state.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_executions_valid_execution_status"), "executions", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_executions_valid_execution_status"),
        "executions",
        "status IN ('QUEUED', 'DISPATCHED', 'RUNNING', 'WAITING_FOR_NEXT_STEP', "
        "'CANCEL_REQUESTED', 'CANCELLED', 'SUCCEEDED', 'FAILED')",
    )
    op.add_column(
        "executions",
        sa.Column(
            "dynamic_finish_requested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.alter_column("executions", "dynamic_finish_requested", server_default=None)

    op.add_column("execution_steps", sa.Column("code", sa.Text(), nullable=True))
    op.add_column(
        "execution_steps", sa.Column("code_hash", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "execution_steps",
        sa.Column("plan_revision_id", sa.String(length=255), nullable=True),
    )

    op.drop_constraint(
        op.f("ck_execution_attempts_valid_attempt_status"),
        "execution_attempts",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_execution_attempts_valid_attempt_status"),
        "execution_attempts",
        "status IN ('RUNNING', 'WAITING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
    )
    op.alter_column("execution_attempts", "lease_owner", nullable=True)
    op.alter_column("execution_attempts", "lease_expires_at", nullable=True)


def downgrade() -> None:
    op.execute(
        "UPDATE execution_attempts SET status = 'FAILED', "
        "error_message = 'Dynamic execution interrupted by schema downgrade', "
        "finished_at = CURRENT_TIMESTAMP, lease_owner = 'schema-downgrade', "
        "lease_expires_at = CURRENT_TIMESTAMP WHERE status = 'WAITING'"
    )
    op.execute(
        "UPDATE executions SET status = 'FAILED', "
        "error_message = 'Dynamic execution interrupted by schema downgrade', "
        "finished_at = CURRENT_TIMESTAMP WHERE status = 'WAITING_FOR_NEXT_STEP'"
    )
    op.alter_column("execution_attempts", "lease_expires_at", nullable=False)
    op.alter_column("execution_attempts", "lease_owner", nullable=False)
    op.drop_constraint(
        op.f("ck_execution_attempts_valid_attempt_status"),
        "execution_attempts",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_execution_attempts_valid_attempt_status"),
        "execution_attempts",
        "status IN ('RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
    )

    op.drop_column("execution_steps", "plan_revision_id")
    op.drop_column("execution_steps", "code_hash")
    op.drop_column("execution_steps", "code")
    op.drop_column("executions", "dynamic_finish_requested")
    op.drop_constraint(
        op.f("ck_executions_valid_execution_status"), "executions", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_executions_valid_execution_status"),
        "executions",
        "status IN ('QUEUED', 'DISPATCHED', 'RUNNING', 'CANCEL_REQUESTED', "
        "'CANCELLED', 'SUCCEEDED', 'FAILED')",
    )
