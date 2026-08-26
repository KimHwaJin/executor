"""Add exclusive cancellation ownership fields.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-26
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
            "cancellation_lease_owner",
            sa.String(length=255),
            nullable=True,
        ),
    )
    op.add_column(
        "executions",
        sa.Column(
            "cancellation_lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "executions",
        sa.Column(
            "cancellation_heartbeat_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_executions_cancellation_lease",
        "executions",
        ["status", "cancellation_lease_expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_executions_cancellation_lease",
        table_name="executions",
    )
    op.drop_column("executions", "cancellation_heartbeat_at")
    op.drop_column("executions", "cancellation_lease_expires_at")
    op.drop_column("executions", "cancellation_lease_owner")
