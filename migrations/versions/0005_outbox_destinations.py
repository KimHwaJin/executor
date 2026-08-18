"""split durable outbox delivery into work and integration event streams

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "outbox_events",
        sa.Column("destination", sa.String(length=32), nullable=False, server_default="EVENTS"),
    )
    op.create_check_constraint(
        op.f("ck_outbox_events_valid_outbox_destination"),
        "outbox_events",
        "destination IN ('WORK', 'EVENTS')",
    )
    op.alter_column("outbox_events", "destination", server_default=None)


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_outbox_events_valid_outbox_destination"),
        "outbox_events",
        type_="check",
    )
    op.drop_column("outbox_events", "destination")
