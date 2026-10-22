"""Reduce the Outbox to public events only.

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

_TABLE = "outbox_events"
_SEQUENCE_COLUMNS = ["aggregate_type", "aggregate_id", "event_sequence"]


def upgrade() -> None:
    # Worker wake-ups (destination WORK) are no longer written or published.
    op.execute(sa.text("DELETE FROM outbox_events WHERE destination = 'WORK'"))
    op.drop_constraint(
        op.f("ck_outbox_events_valid_outbox_content"), _TABLE, type_="check"
    )
    op.drop_constraint(
        op.f("ck_outbox_events_valid_outbox_destination"),
        _TABLE,
        type_="check",
    )
    op.drop_constraint(
        op.f("uq_outbox_aggregate_event_sequence"), _TABLE, type_="unique"
    )
    op.drop_index("ix_outbox_pending_event_order", table_name=_TABLE)
    # The new unique constraint indexes the same columns.
    op.drop_index("ix_outbox_execution_cursor", table_name=_TABLE)
    op.drop_column(_TABLE, "destination")
    op.drop_column(_TABLE, "payload")
    op.alter_column(
        _TABLE, "event_sequence", existing_type=sa.BigInteger(), nullable=False
    )
    op.alter_column(
        _TABLE, "execution_event_id", existing_type=sa.Uuid(), nullable=False
    )
    op.create_check_constraint(
        op.f("ck_outbox_events_valid_outbox_sequence"),
        _TABLE,
        "event_sequence >= 1",
    )
    op.create_unique_constraint(
        op.f("uq_outbox_aggregate_event_sequence"), _TABLE, _SEQUENCE_COLUMNS
    )
    op.create_index(
        "ix_outbox_pending_event_order",
        _TABLE,
        _SEQUENCE_COLUMNS,
        unique=False,
        postgresql_where=sa.text("status = 'PENDING'"),
        sqlite_where=sa.text("status = 'PENDING'"),
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_pending_event_order", table_name=_TABLE)
    op.drop_constraint(
        op.f("uq_outbox_aggregate_event_sequence"), _TABLE, type_="unique"
    )
    op.drop_constraint(
        op.f("ck_outbox_events_valid_outbox_sequence"), _TABLE, type_="check"
    )
    op.alter_column(
        _TABLE, "execution_event_id", existing_type=sa.Uuid(), nullable=True
    )
    op.alter_column(
        _TABLE, "event_sequence", existing_type=sa.BigInteger(), nullable=True
    )
    op.add_column(
        _TABLE, sa.Column("payload", sa.JSON(none_as_null=True), nullable=True)
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "destination",
            sa.Enum(
                "WORK",
                "EVENTS",
                name="outbox_destination",
                native_enum=False,
                length=32,
            ),
            nullable=False,
            server_default="EVENTS",
        ),
    )
    op.alter_column(_TABLE, "destination", server_default=None)
    op.create_index(
        "ix_outbox_execution_cursor", _TABLE, _SEQUENCE_COLUMNS, unique=False
    )
    op.create_index(
        "ix_outbox_pending_event_order",
        _TABLE,
        _SEQUENCE_COLUMNS,
        unique=False,
        postgresql_where=sa.text(
            "destination = 'EVENTS' AND status = 'PENDING'"
        ),
        sqlite_where=sa.text("destination = 'EVENTS' AND status = 'PENDING'"),
    )
    op.create_unique_constraint(
        op.f("uq_outbox_aggregate_event_sequence"),
        _TABLE,
        ["aggregate_type", "aggregate_id", "destination", "event_sequence"],
    )
    op.create_check_constraint(
        op.f("ck_outbox_events_valid_outbox_destination"),
        _TABLE,
        "destination IN ('WORK', 'EVENTS')",
    )
    op.create_check_constraint(
        op.f("ck_outbox_events_valid_outbox_content"),
        _TABLE,
        "(destination = 'EVENTS' AND event_sequence >= 1 "
        "AND execution_event_id IS NOT NULL AND payload IS NULL) OR "
        "(destination = 'WORK' AND event_sequence IS NULL "
        "AND execution_event_id IS NULL AND payload IS NOT NULL)",
    )
