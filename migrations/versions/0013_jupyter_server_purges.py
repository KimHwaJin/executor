"""Add immutable audit tombstones for Jupyter server hard purge.

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "jupyter_server_purges",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("server_id", sa.Uuid(), nullable=False),
        sa.Column("server_name", sa.String(length=255), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("pool", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_by_type", sa.String(length=32), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("updated_by_type", sa.String(length=32), nullable=True),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "pool IN ('INTERACTIVE', 'BATCH')",
            name=op.f("ck_jupyter_server_purges_valid_pool"),
        ),
        sa.CheckConstraint(
            "created_by_type IS NULL OR created_by_type IN ('USER', 'BATCH')",
            name=op.f("ck_jupyter_server_purges_valid_created_by_type"),
        ),
        sa.CheckConstraint(
            "updated_by_type IS NULL OR updated_by_type IN ('USER', 'BATCH')",
            name=op.f("ck_jupyter_server_purges_valid_updated_by_type"),
        ),
        sa.CheckConstraint(
            "(created_by_type IS NULL) = (created_by IS NULL)",
            name=op.f("ck_jupyter_server_purges_complete_created_by"),
        ),
        sa.CheckConstraint(
            "(updated_by_type IS NULL) = (updated_by IS NULL)",
            name=op.f("ck_jupyter_server_purges_complete_updated_by"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jupyter_server_purges")),
        sa.UniqueConstraint(
            "server_id", name=op.f("uq_jupyter_server_purges_server_id")
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name=op.f("uq_jupyter_server_purges_idempotency_key"),
        ),
    )


def downgrade() -> None:
    op.drop_table("jupyter_server_purges")
