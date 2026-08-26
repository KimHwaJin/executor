"""Bridge existing 0001 databases to durable Execution event history.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-26

The pre-release 0001 baseline gained durable event tables before the first Kubernetes
deployment was declared complete.  A database stamped with the earlier 0001 shape would
otherwise never receive those additions.  This bridge is intentionally idempotent: a fresh
database created by the current 0001 already has the target shape, while an earlier 0001
database is upgraded in place and preserves public event IDs and payloads.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "event_retention_lease" not in tables:
        created_at = datetime.now(UTC)
        retention_table = op.create_table(
            "event_retention_lease",
            sa.Column("singleton_key", sa.String(length=32), nullable=False),
            sa.Column("lease_owner", sa.String(length=255), nullable=True),
            sa.Column(
                "lease_expires_at", sa.DateTime(timezone=True), nullable=True
            ),
            sa.Column(
                "last_started_at", sa.DateTime(timezone=True), nullable=True
            ),
            sa.Column(
                "last_completed_at", sa.DateTime(timezone=True), nullable=True
            ),
            sa.Column("last_error", sa.String(length=1000), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False
            ),
            sa.CheckConstraint(
                "(lease_owner IS NULL) = (lease_expires_at IS NULL)",
                name=op.f("ck_event_retention_lease_complete_lease"),
            ),
            sa.PrimaryKeyConstraint(
                "singleton_key", name=op.f("pk_event_retention_lease")
            ),
        )
        op.bulk_insert(
            retention_table,
            [
                {
                    "singleton_key": "events",
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "last_started_at": None,
                    "last_completed_at": None,
                    "last_error": None,
                    "created_at": created_at,
                    "updated_at": created_at,
                }
            ],
        )

    tables = set(sa.inspect(bind).get_table_names())
    if "execution_events" not in tables:
        op.create_table(
            "execution_events",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("execution_id", sa.Uuid(), nullable=False),
            sa.Column("event_sequence", sa.BigInteger(), nullable=False),
            sa.Column("event_type", sa.String(length=255), nullable=False),
            sa.Column("schema_version", sa.String(length=32), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column(
                "created_by_type",
                sa.Enum(
                    "AGENT",
                    "USER",
                    "BATCH",
                    name="actor_type",
                    native_enum=False,
                    length=32,
                ),
                nullable=True,
            ),
            sa.Column("created_by", sa.String(length=255), nullable=True),
            sa.Column(
                "updated_by_type",
                sa.Enum(
                    "AGENT",
                    "USER",
                    "BATCH",
                    name="actor_type",
                    native_enum=False,
                    length=32,
                ),
                nullable=True,
            ),
            sa.Column("updated_by", sa.String(length=255), nullable=True),
            sa.Column("traceparent", sa.String(length=512), nullable=True),
            sa.Column("tracestate", sa.Text(), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False
            ),
            sa.CheckConstraint(
                "created_by_type IS NULL OR created_by_type IN "
                "('AGENT', 'USER', 'BATCH')",
                name=op.f("ck_execution_events_valid_created_by_type"),
            ),
            sa.CheckConstraint(
                "updated_by_type IS NULL OR updated_by_type IN "
                "('AGENT', 'USER', 'BATCH')",
                name=op.f("ck_execution_events_valid_updated_by_type"),
            ),
            sa.CheckConstraint(
                "(created_by_type IS NULL) = (created_by IS NULL)",
                name=op.f("ck_execution_events_complete_created_by"),
            ),
            sa.CheckConstraint(
                "(updated_by_type IS NULL) = (updated_by IS NULL)",
                name=op.f("ck_execution_events_complete_updated_by"),
            ),
            sa.CheckConstraint(
                "event_sequence >= 1",
                name=op.f("ck_execution_events_positive_event_sequence"),
            ),
            sa.ForeignKeyConstraint(
                ["execution_id"],
                ["executions.id"],
                name=op.f("fk_execution_events_execution_id_executions"),
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_execution_events")),
            sa.UniqueConstraint(
                "execution_id",
                "event_sequence",
                name=op.f("uq_execution_events_execution_sequence"),
            ),
        )
        op.create_index(
            "ix_execution_events_created",
            "execution_events",
            ["created_at", "id"],
            unique=False,
        )
        op.create_index(
            "ix_execution_events_execution_cursor",
            "execution_events",
            ["execution_id", "event_sequence"],
            unique=False,
        )

    outbox_columns = {
        column["name"]
        for column in sa.inspect(bind).get_columns("outbox_events")
    }
    if "execution_event_id" not in outbox_columns:
        op.add_column(
            "outbox_events",
            sa.Column("execution_event_id", sa.Uuid(), nullable=True),
        )
        op.alter_column(
            "outbox_events",
            "payload",
            existing_type=sa.JSON(),
            nullable=True,
        )
        op.execute(
            sa.text(
                """
                INSERT INTO execution_events (
                    id, execution_id, event_sequence, event_type,
                    schema_version, payload, created_by_type, created_by,
                    updated_by_type, updated_by, traceparent, tracestate,
                    created_at, updated_at
                )
                SELECT
                    id, aggregate_id, event_sequence, event_type,
                    '1.0', payload, created_by_type, created_by,
                    updated_by_type, updated_by, traceparent, tracestate,
                    created_at, updated_at
                FROM outbox_events
                WHERE destination = 'EVENTS'
                ON CONFLICT (id) DO NOTHING
                """
            )
        )
        op.execute(
            sa.text(
                """
                UPDATE outbox_events
                SET execution_event_id = id, payload = NULL
                WHERE destination = 'EVENTS'
                """
            )
        )

    inspector = sa.inspect(bind)
    check_names = {
        constraint["name"]
        for constraint in inspector.get_check_constraints("outbox_events")
    }
    if "ck_outbox_events_valid_outbox_event_sequence" in check_names:
        op.drop_constraint(
            op.f("ck_outbox_events_valid_outbox_event_sequence"),
            "outbox_events",
            type_="check",
        )
    if "ck_outbox_events_valid_outbox_content" not in check_names:
        op.create_check_constraint(
            op.f("ck_outbox_events_valid_outbox_content"),
            "outbox_events",
            "(destination = 'EVENTS' AND event_sequence >= 1 "
            "AND execution_event_id IS NOT NULL AND payload IS NULL) OR "
            "(destination = 'WORK' AND event_sequence IS NULL "
            "AND execution_event_id IS NULL AND payload IS NOT NULL)",
        )

    inspector = sa.inspect(bind)
    unique_names = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("outbox_events")
    }
    if "uq_outbox_execution_event_id" not in unique_names:
        op.create_unique_constraint(
            op.f("uq_outbox_execution_event_id"),
            "outbox_events",
            ["execution_event_id"],
        )

    inspector = sa.inspect(bind)
    foreign_key_names = {
        constraint["name"]
        for constraint in inspector.get_foreign_keys("outbox_events")
    }
    if "fk_outbox_events_execution_event_id_execution_events" not in (
        foreign_key_names
    ):
        op.create_foreign_key(
            op.f("fk_outbox_events_execution_event_id_execution_events"),
            "outbox_events",
            "execution_events",
            ["execution_event_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    # The current 0001 baseline already describes the durable-event shape.  Keeping
    # the bridge downgrade non-destructive preserves that baseline contract.
    pass
