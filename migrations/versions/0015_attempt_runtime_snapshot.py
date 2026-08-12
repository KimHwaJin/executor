"""Snapshot Runtime identity on every Execution Attempt.

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "execution_attempts",
        sa.Column("runtime_type", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "execution_attempts",
        sa.Column("runtime_profile", sa.String(length=128), nullable=True),
    )
    op.execute(
        "UPDATE execution_attempts AS attempt "
        "SET runtime_type = execution.runtime_type, "
        "runtime_profile = execution.runtime_profile "
        "FROM executions AS execution "
        "WHERE attempt.execution_id = execution.id"
    )
    op.alter_column("execution_attempts", "runtime_type", nullable=False)
    op.alter_column("execution_attempts", "runtime_profile", nullable=False)
    op.create_check_constraint(
        op.f("ck_execution_attempts_valid_attempt_runtime_type"),
        "execution_attempts",
        "runtime_type IN ('JUPYTER')",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_execution_attempts_valid_attempt_runtime_type"),
        "execution_attempts",
        type_="check",
    )
    op.drop_column("execution_attempts", "runtime_profile")
    op.drop_column("execution_attempts", "runtime_type")
