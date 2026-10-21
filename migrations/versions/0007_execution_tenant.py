"""Add the Execution tenant and make task_id optional.

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable additions only: Workers on 0006 keep running during a
    # rolling update and never read tenant_id.
    op.add_column("executions", sa.Column("tenant_id", sa.String(length=64)))
    op.create_index(
        "ix_executions_tenant_created_cursor",
        "executions",
        ["tenant_id", "created_at", "id"],
    )
    op.alter_column(
        "executions",
        "task_id",
        existing_type=sa.String(length=255),
        nullable=True,
    )


def downgrade() -> None:
    # Fails while Executions without a task_id exist; resolve them first.
    op.alter_column(
        "executions",
        "task_id",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.drop_index(
        "ix_executions_tenant_created_cursor", table_name="executions"
    )
    op.drop_column("executions", "tenant_id")
