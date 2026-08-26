"""Add persistent Executor-wide maintenance state.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-26
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    created_at = datetime.now(UTC)
    table = op.create_table(
        "executor_maintenance",
        sa.Column("singleton_key", sa.String(length=32), nullable=False),
        sa.Column(
            "admission_state",
            sa.Enum(
                "ACTIVE",
                "DRAINING",
                name="executor_admission_state",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("created_by_type", sa.String(length=32), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("updated_by_type", sa.String(length=32), nullable=True),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "singleton_key = 'executor'",
            name=op.f("ck_executor_maintenance_singleton_key"),
        ),
        sa.CheckConstraint(
            "admission_state IN ('ACTIVE', 'DRAINING')",
            name=op.f("ck_executor_maintenance_valid_admission_state"),
        ),
        sa.CheckConstraint(
            "version >= 0",
            name=op.f("ck_executor_maintenance_non_negative_version"),
        ),
        sa.CheckConstraint(
            "created_by_type IS NULL OR created_by_type IN "
            "('AGENT', 'USER', 'BATCH')",
            name=op.f("ck_executor_maintenance_valid_created_by_type"),
        ),
        sa.CheckConstraint(
            "updated_by_type IS NULL OR updated_by_type IN "
            "('AGENT', 'USER', 'BATCH')",
            name=op.f("ck_executor_maintenance_valid_updated_by_type"),
        ),
        sa.CheckConstraint(
            "(created_by_type IS NULL) = (created_by IS NULL)",
            name=op.f("ck_executor_maintenance_complete_created_by"),
        ),
        sa.CheckConstraint(
            "(updated_by_type IS NULL) = (updated_by IS NULL)",
            name=op.f("ck_executor_maintenance_complete_updated_by"),
        ),
        sa.PrimaryKeyConstraint(
            "singleton_key", name=op.f("pk_executor_maintenance")
        ),
    )
    op.bulk_insert(
        table,
        [
            {
                "singleton_key": "executor",
                "admission_state": "ACTIVE",
                "version": 0,
                "created_by_type": None,
                "created_by": None,
                "updated_by_type": None,
                "updated_by": None,
                "created_at": created_at,
                "updated_at": created_at,
            }
        ],
    )


def downgrade() -> None:
    op.drop_table("executor_maintenance")
