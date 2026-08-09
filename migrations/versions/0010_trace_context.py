"""Persist W3C trace context across asynchronous execution boundaries.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "executions", sa.Column("traceparent", sa.String(length=512), nullable=True)
    )
    op.add_column("executions", sa.Column("tracestate", sa.Text(), nullable=True))
    op.add_column(
        "outbox_events", sa.Column("traceparent", sa.String(length=512), nullable=True)
    )
    op.add_column("outbox_events", sa.Column("tracestate", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("outbox_events", "tracestate")
    op.drop_column("outbox_events", "traceparent")
    op.drop_column("executions", "tracestate")
    op.drop_column("executions", "traceparent")
