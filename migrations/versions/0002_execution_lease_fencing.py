"""Add monotonic lease fencing tokens.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "executions",
        sa.Column(
            "fencing_token",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_executions_non_negative_fencing_token"),
        "executions",
        "fencing_token >= 0",
    )
    op.add_column(
        "execution_attempts",
        sa.Column(
            "fencing_token",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_execution_attempts_non_negative_fencing_token"),
        "execution_attempts",
        "fencing_token >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_execution_attempts_non_negative_fencing_token"),
        "execution_attempts",
        type_="check",
    )
    op.drop_column("execution_attempts", "fencing_token")
    op.drop_constraint(
        op.f("ck_executions_non_negative_fencing_token"),
        "executions",
        type_="check",
    )
    op.drop_column("executions", "fencing_token")
