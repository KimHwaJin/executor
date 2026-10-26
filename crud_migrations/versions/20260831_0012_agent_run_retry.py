"""Add durable AgentRun retry scheduling fields.

Revision ID: 20260831_0012
Revises: 20260831_0011
Create Date: 2026-08-31
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260831_0012"
down_revision: Union[str, None] = "20260831_0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "agent_runs",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_agent_runs_next_attempt_at", "agent_runs", ["next_attempt_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_agent_runs_next_attempt_at", table_name="agent_runs")
    op.drop_column("agent_runs", "next_attempt_at")
    op.drop_column("agent_runs", "attempt_count")

