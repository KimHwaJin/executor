"""Link CRUD tasks to LangGraph and Executor task identities.

Revision ID: 20260907_0017
Revises: 20260904_0016
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260907_0017"
down_revision: Union[str, None] = "20260904_0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("graph_task_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "uq_tasks_graph_task_id",
        "tasks",
        ["graph_task_id"],
        unique=True,
        postgresql_where=sa.text("graph_task_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_tasks_graph_task_id", table_name="tasks")
    op.drop_column("tasks", "graph_task_id")
