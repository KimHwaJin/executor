"""Record the tenant of each Execution binding.

The Worker subscribes only to tenant Streams and routes by the binding's
tenant. Bindings the Worker creates for claimed Executions have no Agent
task, so task_id becomes nullable. Rows written before this revision keep
tenant NULL and are never routed again.
"""

import sqlalchemy as sa
from alembic import op

revision = "ew_0007"
down_revision = "ew_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ew_bindings", sa.Column("tenant", sa.Text()))
    op.alter_column("ew_bindings", "task_id", nullable=True)


def downgrade() -> None:
    # Claimed Executions cannot exist without task_id: drop them entirely.
    claimed = (
        "SELECT namespace, execution_id FROM ew_bindings WHERE task_id IS NULL"
    )
    op.execute(
        "DELETE FROM ew_outbox o USING ew_commands c "
        "WHERE o.namespace=c.namespace AND o.command_id=c.command_id "
        f"AND (c.namespace, c.execution_id) IN ({claimed})"
    )
    for table in ("ew_commands", "ew_inbox"):
        op.execute(
            f"DELETE FROM {table} WHERE (namespace, execution_id) IN ({claimed})"
        )
    op.execute("DELETE FROM ew_bindings WHERE task_id IS NULL")
    op.alter_column("ew_bindings", "task_id", nullable=False)
    op.drop_column("ew_bindings", "tenant")
