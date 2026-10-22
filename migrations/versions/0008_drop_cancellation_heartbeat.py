"""Drop the unused Execution cancellation heartbeat column.

Revision ID: 0008
Revises: 0007
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Cancellation liveness is cancellation_lease_expires_at; nothing read
    # this timestamp. Stop 0007 Workers before upgrading.
    op.drop_column("executions", "cancellation_heartbeat_at")


def downgrade() -> None:
    op.add_column(
        "executions",
        sa.Column(
            "cancellation_heartbeat_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
