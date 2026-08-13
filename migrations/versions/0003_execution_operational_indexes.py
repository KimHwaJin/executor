"""add execution operational indexes

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-13
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_executions_created_cursor",
        "executions",
        ["created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_executions_retained_session_cleanup",
        "executions",
        ["status", "retry_strategy", "retained_runtime_session_until"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_executions_retained_session_cleanup", table_name="executions")
    op.drop_index("ix_executions_created_cursor", table_name="executions")
