"""Record each Step attempt's kernel peak memory and CPU time.

Both columns are nullable: Steps that ran before this release, or on a
Jupyter image without the measurement, have no values.

Revision ID: 0017
Revises: 0016
"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "execution_step_attempts",
        sa.Column("peak_memory_bytes", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "execution_step_attempts",
        sa.Column("cpu_seconds", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("execution_step_attempts", "cpu_seconds")
    op.drop_column("execution_step_attempts", "peak_memory_bytes")
