"""Adopt Agent Task/Plan references and ExecutionSpec source snapshots.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_executions_correlation_id", table_name="executions")
    op.alter_column("executions", "correlation_id", new_column_name="task_id")
    op.execute(
        "UPDATE executions SET task_id = 'legacy-task-' || CAST(id AS VARCHAR) "
        "WHERE task_id IS NULL"
    )
    op.alter_column("executions", "task_id", existing_type=sa.String(255), nullable=False)
    op.create_index("ix_executions_task_id", "executions", ["task_id"], unique=False)

    op.add_column(
        "executions", sa.Column("source_sha256", sa.String(length=64), nullable=True)
    )
    op.execute("UPDATE executions SET code = '{\"schema_version\":\"legacy\"}' WHERE code IS NULL")
    op.execute("UPDATE executions SET source_sha256 = repeat('0', 64)")
    op.alter_column("executions", "code", existing_type=sa.Text(), nullable=False)
    op.alter_column(
        "executions", "source_sha256", existing_type=sa.String(64), nullable=False
    )
    op.drop_constraint(op.f("ck_executions_valid_code_source"), "executions", type_="check")
    op.create_check_constraint(
        op.f("ck_executions_valid_code_source"),
        "executions",
        "(code_source_type = 'INLINE' AND code_path IS NULL) OR "
        "(code_source_type = 'PATH' AND code_path IS NOT NULL)",
    )

    op.alter_column(
        "execution_steps", "plan_revision_id", new_column_name="execution_plan_id"
    )
    op.add_column(
        "execution_steps", sa.Column("plan_step_id", sa.String(length=255), nullable=True)
    )
    op.execute(
        "UPDATE execution_steps AS step "
        "SET execution_plan_id = execution.execution_plan_id "
        "FROM executions AS execution "
        "WHERE step.execution_id = execution.id AND step.execution_plan_id IS NULL"
    )
    op.execute(
        "UPDATE execution_steps SET plan_step_id = 'legacy-step-' || CAST(id AS VARCHAR), "
        "code = COALESCE(code, '# Legacy source retained on execution record')"
    )
    op.alter_column(
        "execution_steps", "execution_plan_id", existing_type=sa.String(255), nullable=False
    )
    op.alter_column(
        "execution_steps", "plan_step_id", existing_type=sa.String(255), nullable=False
    )
    op.alter_column("execution_steps", "code", existing_type=sa.Text(), nullable=False)
    op.create_index(
        "ix_execution_steps_plan_step_id",
        "execution_steps",
        ["plan_step_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_execution_steps_plan_step_id", table_name="execution_steps")
    op.alter_column("execution_steps", "code", existing_type=sa.Text(), nullable=True)
    op.alter_column(
        "execution_steps", "execution_plan_id", existing_type=sa.String(255), nullable=True
    )
    op.drop_column("execution_steps", "plan_step_id")
    op.alter_column(
        "execution_steps", "execution_plan_id", new_column_name="plan_revision_id"
    )

    op.drop_constraint(op.f("ck_executions_valid_code_source"), "executions", type_="check")
    op.execute("UPDATE executions SET code = NULL WHERE code_source_type = 'PATH'")
    op.create_check_constraint(
        op.f("ck_executions_valid_code_source"),
        "executions",
        "(code_source_type = 'INLINE' AND code IS NOT NULL AND code_path IS NULL) OR "
        "(code_source_type = 'PATH' AND code IS NULL AND code_path IS NOT NULL)",
    )
    op.alter_column("executions", "code", existing_type=sa.Text(), nullable=True)
    op.drop_column("executions", "source_sha256")
    op.drop_index("ix_executions_task_id", table_name="executions")
    op.alter_column("executions", "task_id", existing_type=sa.String(255), nullable=True)
    op.alter_column("executions", "task_id", new_column_name="correlation_id")
    op.create_index(
        "ix_executions_correlation_id", "executions", ["correlation_id"], unique=False
    )
