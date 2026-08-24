"""Persist bounded Runtime abort outcomes.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VALID_ABORT_STATUS = (
    "runtime_abort_status IN ('NOT_REQUIRED', 'PENDING', "
    "'IDLE_CONFIRMED', 'SESSION_DELETED', 'SESSION_MISSING', 'FAILED')"
)


def upgrade() -> None:
    op.add_column(
        "executions",
        sa.Column(
            "runtime_abort_status",
            sa.String(length=32),
            server_default="NOT_REQUIRED",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_executions_valid_runtime_abort_status"),
        "executions",
        _VALID_ABORT_STATUS,
    )
    op.add_column(
        "execution_attempts",
        sa.Column(
            "runtime_abort_status",
            sa.String(length=32),
            server_default="NOT_REQUIRED",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_execution_attempts_valid_runtime_abort_status"),
        "execution_attempts",
        _VALID_ABORT_STATUS,
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_execution_attempts_valid_runtime_abort_status"),
        "execution_attempts",
        type_="check",
    )
    op.drop_column("execution_attempts", "runtime_abort_status")
    op.drop_constraint(
        op.f("ck_executions_valid_runtime_abort_status"),
        "executions",
        type_="check",
    )
    op.drop_column("executions", "runtime_abort_status")
