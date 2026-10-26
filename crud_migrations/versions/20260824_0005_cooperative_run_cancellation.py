"""Add cooperative cancellation request timestamps.

Revision ID: 20260824_0005
Revises: 20260821_0004
Create Date: 2026-08-24
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260824_0005"
down_revision: Union[str, None] = "20260821_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 요청 시각을 먼저 기록하고 Worker가 멈춘 뒤 기존 canceled 상태로 확정합니다.
    op.add_column(
        "agent_runs",
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_agent_runs_cancel_requested_at",
        "agent_runs",
        ["cancel_requested_at"],
    )
    op.create_index(
        "ix_tasks_cancel_requested_at",
        "tasks",
        ["cancel_requested_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_tasks_cancel_requested_at", table_name="tasks")
    op.drop_index("ix_agent_runs_cancel_requested_at", table_name="agent_runs")
    op.drop_column("tasks", "cancel_requested_at")
    op.drop_column("agent_runs", "cancel_requested_at")

