"""Store each Execution's kernel memory limit and encrypted kernel env.

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
    # rolling update and never read them.
    op.add_column(
        "executions", sa.Column("memory_limit_bytes", sa.BigInteger())
    )
    op.add_column("executions", sa.Column("runtime_env_ciphertext", sa.Text()))
    op.create_check_constraint(
        op.f("ck_executions_positive_memory_limit_bytes"),
        "executions",
        "memory_limit_bytes IS NULL OR memory_limit_bytes > 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_executions_positive_memory_limit_bytes"),
        "executions",
        type_="check",
    )
    op.drop_column("executions", "runtime_env_ciphertext")
    op.drop_column("executions", "memory_limit_bytes")
