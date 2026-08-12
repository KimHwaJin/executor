"""persist runtime target resource observations

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runtime_targets", sa.Column("resource_observed_at", sa.DateTime(timezone=True)))
    op.add_column("runtime_targets", sa.Column("resource_last_check_at", sa.DateTime(timezone=True)))
    op.add_column("runtime_targets", sa.Column("resource_last_error", sa.String(length=500)))
    op.add_column("runtime_targets", sa.Column("resource_source", sa.String(length=64)))
    op.add_column("runtime_targets", sa.Column("resource_estimated", sa.Boolean()))
    op.add_column("runtime_targets", sa.Column("resource_process_count", sa.Integer()))
    op.add_column("runtime_targets", sa.Column("cpu_used_cores", sa.Float()))
    op.add_column("runtime_targets", sa.Column("cpu_capacity_cores", sa.Float()))
    op.add_column("runtime_targets", sa.Column("cpu_utilization", sa.Float()))
    op.add_column("runtime_targets", sa.Column("memory_used_bytes", sa.BigInteger()))
    op.add_column("runtime_targets", sa.Column("memory_capacity_bytes", sa.BigInteger()))
    op.add_column("runtime_targets", sa.Column("memory_utilization", sa.Float()))
    op.add_column(
        "runtime_targets",
        sa.Column("resource_errors", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.alter_column("runtime_targets", "resource_errors", server_default=None)


def downgrade() -> None:
    for column in (
        "resource_errors",
        "memory_utilization",
        "memory_capacity_bytes",
        "memory_used_bytes",
        "cpu_utilization",
        "cpu_capacity_cores",
        "cpu_used_cores",
        "resource_process_count",
        "resource_estimated",
        "resource_source",
        "resource_last_error",
        "resource_last_check_at",
        "resource_observed_at",
    ):
        op.drop_column("runtime_targets", column)
