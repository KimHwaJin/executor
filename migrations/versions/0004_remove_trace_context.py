"""Remove approved obsolete trace fields, preserving business history.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_TABLES = ("executions", "execution_events", "outbox_events")


def upgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "traceparent")
        op.drop_column(table, "tracestate")


def downgrade() -> None:
    # Restore the old schema, not the intentionally discarded trace values.
    for table in reversed(_TABLES):
        op.add_column(
            table, sa.Column("traceparent", sa.String(512), nullable=True)
        )
        op.add_column(table, sa.Column("tracestate", sa.Text(), nullable=True))
