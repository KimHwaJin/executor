"""Track Jupyter journal requests and Step handover between Workers.

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # New tables only: Workers on 0005 keep running during a rolling
    # update and never read them.
    op.create_table(
        "execution_step_runs",
        sa.Column("execution_step_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_baseline", sa.JSON(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "execution_step_attempt_id", name=op.f("pk_execution_step_runs")
        ),
        sa.ForeignKeyConstraint(
            ["execution_step_attempt_id"],
            ["execution_step_attempts.id"],
            ondelete="CASCADE",
            name=op.f(
                "fk_execution_step_runs_execution_step_attempt_id_"
                "execution_step_attempts"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["executions.id"],
            ondelete="CASCADE",
            name=op.f("fk_execution_step_runs_execution_id_executions"),
        ),
        sa.UniqueConstraint(
            "request_id", name=op.f("uq_execution_step_runs_request_id")
        ),
    )
    op.create_index(
        op.f("ix_execution_step_runs_execution_id"),
        "execution_step_runs",
        ["execution_id"],
    )
    op.create_table(
        "execution_handovers",
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("paused_since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "execution_id", name=op.f("pk_execution_handovers")
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["executions.id"],
            ondelete="CASCADE",
            name=op.f("fk_execution_handovers_execution_id_executions"),
        ),
    )


def downgrade() -> None:
    op.drop_table("execution_handovers")
    op.drop_table("execution_step_runs")
