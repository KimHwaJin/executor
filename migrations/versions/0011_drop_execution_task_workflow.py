"""Drop the Execution task_id and workflow_id.

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Callers name their own hierarchy in metadata. 0010 Pods still write
    # these columns, so drain them (or deploy with Recreate) first.
    op.drop_index("ix_executions_task_created_cursor", table_name="executions")
    op.drop_index(op.f("ix_executions_task_id"), table_name="executions")
    op.drop_column("executions", "task_id")
    op.drop_column("executions", "workflow_id")
    # Stored completion payloads are revalidated on publish against the
    # context summary that no longer has these keys.
    op.execute(
        sa.text(
            "UPDATE execution_events SET payload = (payload::jsonb"
            " #- '{execution,context,task_id}'"
            " #- '{execution,context,workflow_id}')::json"
            " WHERE event_type = 'execution.completed'"
            " AND payload::jsonb #> '{execution,context}' IS NOT NULL"
        )
    )


def downgrade() -> None:
    # Values dropped by upgrade are not restored.
    op.add_column(
        "executions",
        sa.Column("workflow_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "executions",
        sa.Column("task_id", sa.String(length=255), nullable=True),
    )
    op.create_index(
        op.f("ix_executions_task_id"), "executions", ["task_id"], unique=False
    )
    op.create_index(
        "ix_executions_task_created_cursor",
        "executions",
        ["task_id", "created_at", "id"],
    )
