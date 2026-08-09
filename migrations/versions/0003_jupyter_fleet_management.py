"""Add encrypted Jupyter fleet management and command receipts.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_jupyter_servers_pool_status", table_name="jupyter_servers")
    op.add_column(
        "jupyter_servers", sa.Column("credential_ciphertext", sa.Text(), nullable=True)
    )
    op.add_column(
        "jupyter_servers",
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
    )
    op.add_column(
        "jupyter_servers",
        sa.Column("last_health_check_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "jupyter_servers", sa.Column("last_health_error", sa.String(length=500), nullable=True)
    )
    op.add_column(
        "jupyter_servers", sa.Column("active_kernel_count", sa.Integer(), nullable=True)
    )
    op.alter_column("jupyter_servers", "enabled", server_default=None)
    op.create_index(
        "ix_jupyter_servers_pool_status",
        "jupyter_servers",
        ["pool", "enabled", "status"],
        unique=False,
    )

    op.create_table(
        "command_receipts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("command_type", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_command_receipts")),
        sa.UniqueConstraint(
            "idempotency_key", name=op.f("uq_command_receipts_idempotency_key")
        ),
    )


def downgrade() -> None:
    op.drop_table("command_receipts")
    op.drop_index("ix_jupyter_servers_pool_status", table_name="jupyter_servers")
    op.drop_column("jupyter_servers", "active_kernel_count")
    op.drop_column("jupyter_servers", "last_health_error")
    op.drop_column("jupyter_servers", "last_health_check_at")
    op.drop_column("jupyter_servers", "enabled")
    op.drop_column("jupyter_servers", "credential_ciphertext")
    op.create_index(
        "ix_jupyter_servers_pool_status",
        "jupyter_servers",
        ["pool", "status"],
        unique=False,
    )
