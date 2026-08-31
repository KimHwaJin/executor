"""Add bounded, append-only execution diagnostic history without data reset.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "execution_diagnostics",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=True),
        sa.Column("operation_id", sa.Uuid(), nullable=True),
        sa.Column("step_id", sa.Uuid(), nullable=True),
        sa.Column("step_sequence", sa.Integer(), nullable=True),
        sa.Column("fencing_token", sa.Integer(), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_type", sa.String(32), nullable=True),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column("updated_by_type", sa.String(32), nullable=True),
        sa.Column("updated_by", sa.String(255), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_execution_diagnostics")),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["executions.id"],
            ondelete="CASCADE",
            name=op.f("fk_execution_diagnostics_execution_id_executions"),
        ),
        sa.CheckConstraint(
            "fencing_token >= 1",
            name=op.f("ck_execution_diagnostics_positive_fencing_token"),
        ),
        sa.CheckConstraint(
            "step_sequence IS NULL OR step_sequence >= 0",
            name=op.f("ck_execution_diagnostics_nonnegative_step_sequence"),
        ),
        sa.CheckConstraint(
            "created_by_type IS NULL OR created_by_type IN ('AGENT', 'USER', 'BATCH')",
            name=op.f("ck_execution_diagnostics_valid_created_by_type"),
        ),
        sa.CheckConstraint(
            "updated_by_type IS NULL OR updated_by_type IN ('AGENT', 'USER', 'BATCH')",
            name=op.f("ck_execution_diagnostics_valid_updated_by_type"),
        ),
        sa.CheckConstraint(
            "(created_by_type IS NULL) = (created_by IS NULL)",
            name=op.f("ck_execution_diagnostics_complete_created_by"),
        ),
        sa.CheckConstraint(
            "(updated_by_type IS NULL) = (updated_by IS NULL)",
            name=op.f("ck_execution_diagnostics_complete_updated_by"),
        ),
    )
    op.create_index(
        "ix_diagnostics_execution_cursor",
        "execution_diagnostics",
        ["execution_id", "created_at", "id"],
    )
    op.create_index(
        "ix_diagnostics_attempt_cursor",
        "execution_diagnostics",
        ["execution_id", "attempt_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_table("execution_diagnostics")
