"""Persist Runtime session-count observation time.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runtime_targets",
        sa.Column(
            "session_count_observed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        op.f("ck_runtime_targets_non_negative_active_session_count"),
        "runtime_targets",
        "active_session_count IS NULL OR active_session_count >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_runtime_targets_non_negative_active_session_count"),
        "runtime_targets",
        type_="check",
    )
    op.drop_column("runtime_targets", "session_count_observed_at")
