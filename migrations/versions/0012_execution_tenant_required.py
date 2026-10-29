"""Require an Execution tenant.

Revision ID: 0012
Revises: 0011
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Every caller key names a tenant now. Executions from before have no
    # owner a caller could prove, so the release starts from an empty
    # database; this fails on rows without a tenant instead of guessing one.
    op.alter_column(
        "executions",
        "tenant_id",
        existing_type=sa.String(length=64),
        nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "executions",
        "tenant_id",
        existing_type=sa.String(length=64),
        nullable=True,
    )
