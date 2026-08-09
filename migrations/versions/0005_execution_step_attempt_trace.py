"""Add immutable Step execution history per Attempt.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "execution_step_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("execution_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("execution_step_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("skill_name", sa.String(length=255), nullable=True),
        sa.Column("tool_name", sa.String(length=255), nullable=True),
        sa.Column("input_parameters", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "RUNNING",
                "SUCCEEDED",
                "FAILED",
                "SKIPPED",
                "CANCELLED",
                name="step_attempt_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("outputs", sa.JSON(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "sequence >= 0",
            name=op.f("ck_execution_step_attempts_non_negative_sequence"),
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', "
            "'SKIPPED', 'CANCELLED')",
            name=op.f("ck_execution_step_attempts_valid_step_attempt_status"),
        ),
        sa.ForeignKeyConstraint(
            ["execution_attempt_id"],
            ["execution_attempts.id"],
            name=op.f(
                "fk_execution_step_attempts_execution_attempt_id_execution_attempts"
            ),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["executions.id"],
            name=op.f("fk_execution_step_attempts_execution_id_executions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["execution_step_id"],
            ["execution_steps.id"],
            name=op.f("fk_execution_step_attempts_execution_step_id_execution_steps"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_execution_step_attempts")),
        sa.UniqueConstraint(
            "execution_attempt_id",
            "sequence",
            name=op.f("uq_execution_step_attempts_attempt_sequence"),
        ),
    )
    op.create_index(
        op.f("ix_execution_step_attempts_execution_attempt_id"),
        "execution_step_attempts",
        ["execution_attempt_id"],
        unique=False,
    )
    op.create_index(
        "ix_step_attempts_execution_sequence",
        "execution_step_attempts",
        ["execution_id", "sequence"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_step_attempts_execution_sequence", table_name="execution_step_attempts"
    )
    op.drop_index(
        op.f("ix_execution_step_attempts_execution_attempt_id"),
        table_name="execution_step_attempts",
    )
    op.drop_table("execution_step_attempts")
