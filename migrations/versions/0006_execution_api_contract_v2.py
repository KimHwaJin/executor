"""refactor execution API persistence for operation contract v2

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_AUDITED_TABLES = (
    "executions",
    "execution_steps",
    "runtime_targets",
    "runtime_target_purges",
    "execution_attempts",
    "execution_step_attempts",
    "execution_artifacts",
    "outbox_events",
)


def _replace_actor_constraints(*, include_agent: bool) -> None:
    allowed = "'AGENT', 'USER', 'BATCH'" if include_agent else "'USER', 'BATCH'"
    for table_name in _AUDITED_TABLES:
        op.drop_constraint(
            op.f(f"ck_{table_name}_valid_created_by_type"),
            table_name,
            type_="check",
        )
        op.drop_constraint(
            op.f(f"ck_{table_name}_valid_updated_by_type"),
            table_name,
            type_="check",
        )
        op.create_check_constraint(
            op.f(f"ck_{table_name}_valid_created_by_type"),
            table_name,
            f"created_by_type IS NULL OR created_by_type IN ({allowed})",
        )
        op.create_check_constraint(
            op.f(f"ck_{table_name}_valid_updated_by_type"),
            table_name,
            f"updated_by_type IS NULL OR updated_by_type IN ({allowed})",
        )


def upgrade() -> None:
    connection = op.get_bind()

    op.drop_constraint(op.f("ck_executions_valid_execution_status"), "executions", type_="check")
    op.drop_constraint(op.f("ck_executions_valid_execution_mode"), "executions", type_="check")
    op.drop_constraint(op.f("ck_executions_valid_failure_type"), "executions", type_="check")
    op.drop_constraint(
        op.f("ck_execution_attempts_valid_attempt_failure_type"),
        "execution_attempts",
        type_="check",
    )

    connection.execute(
        sa.text(
            "UPDATE executions SET status = 'WAITING_FOR_OPERATION' "
            "WHERE status = 'WAITING_FOR_CONTINUE'"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE executions SET mode = CASE mode "
            "WHEN 'STATIC' THEN 'SINGLE' WHEN 'DYNAMIC' THEN 'MULTI' ELSE mode END"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE executions SET failure_type = 'OPERATION_WAIT_TIMEOUT' "
            "WHERE failure_type = 'DYNAMIC_WAIT_TIMEOUT'"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE execution_attempts SET failure_type = 'OPERATION_WAIT_TIMEOUT' "
            "WHERE failure_type = 'DYNAMIC_WAIT_TIMEOUT'"
        )
    )

    op.alter_column("executions", "mode", new_column_name="operation_mode")
    op.add_column(
        "executions", sa.Column("operation_wait_timeout_seconds", sa.Integer(), nullable=True)
    )
    connection.execute(
        sa.text(
            "UPDATE executions SET operation_wait_timeout_seconds = 3600 "
            "WHERE operation_mode = 'MULTI'"
        )
    )
    op.alter_column(
        "executions", "dynamic_finish_requested", new_column_name="finalization_requested"
    )
    op.drop_index(op.f("ix_executions_dynamic_wait_expires_at"), table_name="executions")
    op.alter_column(
        "executions", "dynamic_wait_expires_at", new_column_name="operation_wait_expires_at"
    )
    op.create_index(
        op.f("ix_executions_operation_wait_expires_at"),
        "executions",
        ["operation_wait_expires_at"],
    )
    op.alter_column("executions", "project_id", nullable=True)
    op.alter_column("executions", "session_id", nullable=True)
    op.drop_column("executions", "execution_plan_id")

    op.drop_index(op.f("ix_execution_steps_plan_step_id"), table_name="execution_steps")
    op.add_column("execution_steps", sa.Column("step_timeout_seconds", sa.Integer(), nullable=True))
    op.drop_column("execution_steps", "plan_step_id")
    op.drop_column("execution_steps", "execution_plan_id")

    op.add_column(
        "execution_operations",
        sa.Column("operation_timeout_seconds", sa.Integer(), nullable=True),
    )
    op.add_column("execution_operations", sa.Column("source_content", sa.Text(), nullable=True))
    connection.execute(
        sa.text(
            "UPDATE execution_operations SET source_content = executions.code "
            "FROM executions WHERE executions.id = execution_operations.execution_id"
        )
    )
    op.alter_column("execution_operations", "source_content", nullable=False)
    op.add_column(
        "execution_operations",
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.alter_column("execution_operations", "metadata", server_default=None)
    op.drop_column("execution_operations", "execution_plan_id")

    op.create_check_constraint(
        op.f("ck_executions_valid_execution_status"),
        "executions",
        "status IN ('QUEUED', 'DISPATCHED', 'RUNNING', 'WAITING_FOR_OPERATION', "
        "'FINALIZING', 'CANCEL_REQUESTED', 'CANCELLED', 'SUCCEEDED', 'FAILED')",
    )
    op.create_check_constraint(
        op.f("ck_executions_valid_operation_mode"),
        "executions",
        "operation_mode IN ('SINGLE', 'MULTI')",
    )
    op.create_check_constraint(
        op.f("ck_executions_valid_operation_wait_timeout"),
        "executions",
        "(operation_mode = 'SINGLE' AND operation_wait_timeout_seconds IS NULL) OR "
        "(operation_mode = 'MULTI' AND operation_wait_timeout_seconds >= 30)",
    )
    failure_values = (
        "'TOOL_ERROR', 'INFRASTRUCTURE_ERROR', 'WORKER_SHUTDOWN', "
        "'RUNTIME_UNAVAILABLE', 'LEASE_EXPIRED', 'INTERNAL_ERROR', "
        "'OPERATION_WAIT_TIMEOUT', 'OPERATION_TIMEOUT', 'STEP_TIMEOUT', "
        "'EXECUTION_TIMEOUT', 'RUNTIME_SESSION_LOST'"
    )
    op.create_check_constraint(
        op.f("ck_executions_valid_failure_type"),
        "executions",
        f"failure_type IS NULL OR failure_type IN ({failure_values})",
    )
    op.create_check_constraint(
        op.f("ck_execution_attempts_valid_attempt_failure_type"),
        "execution_attempts",
        f"failure_type IS NULL OR failure_type IN ({failure_values})",
    )
    op.create_check_constraint(
        op.f("ck_execution_steps_valid_step_timeout"),
        "execution_steps",
        "step_timeout_seconds IS NULL OR step_timeout_seconds >= 1",
    )
    op.create_check_constraint(
        op.f("ck_execution_operations_valid_operation_timeout"),
        "execution_operations",
        "operation_timeout_seconds IS NULL OR operation_timeout_seconds >= 1",
    )
    _replace_actor_constraints(include_agent=True)
    op.create_check_constraint(
        op.f("ck_execution_operations_valid_created_by_type"),
        "execution_operations",
        "created_by_type IS NULL OR created_by_type IN ('AGENT', 'USER', 'BATCH')",
    )
    op.create_check_constraint(
        op.f("ck_execution_operations_valid_updated_by_type"),
        "execution_operations",
        "updated_by_type IS NULL OR updated_by_type IN ('AGENT', 'USER', 'BATCH')",
    )
    op.create_check_constraint(
        op.f("ck_execution_operations_complete_created_by"),
        "execution_operations",
        "(created_by_type IS NULL) = (created_by IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_execution_operations_complete_updated_by"),
        "execution_operations",
        "(updated_by_type IS NULL) = (updated_by IS NULL)",
    )

    # Pending v1 integration events and command receipts cannot be interpreted as v2 contracts.
    connection.execute(sa.text("DELETE FROM outbox_events"))
    connection.execute(sa.text("DELETE FROM command_receipts"))


def downgrade() -> None:
    connection = op.get_bind()
    for table_name in (*_AUDITED_TABLES, "execution_operations"):
        connection.execute(
            sa.text(
                f"UPDATE {table_name} SET created_by_type = 'USER' WHERE created_by_type = 'AGENT'"
            )
        )
        connection.execute(
            sa.text(
                f"UPDATE {table_name} SET updated_by_type = 'USER' WHERE updated_by_type = 'AGENT'"
            )
        )
    _replace_actor_constraints(include_agent=False)
    for constraint_name in (
        "valid_created_by_type",
        "valid_updated_by_type",
        "complete_created_by",
        "complete_updated_by",
    ):
        op.drop_constraint(
            op.f(f"ck_execution_operations_{constraint_name}"),
            "execution_operations",
            type_="check",
        )

    op.drop_constraint(
        op.f("ck_execution_operations_valid_operation_timeout"),
        "execution_operations",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_execution_steps_valid_step_timeout"), "execution_steps", type_="check"
    )
    op.drop_constraint(
        op.f("ck_execution_attempts_valid_attempt_failure_type"),
        "execution_attempts",
        type_="check",
    )
    op.drop_constraint(op.f("ck_executions_valid_failure_type"), "executions", type_="check")
    op.drop_constraint(
        op.f("ck_executions_valid_operation_wait_timeout"), "executions", type_="check"
    )
    op.drop_constraint(op.f("ck_executions_valid_operation_mode"), "executions", type_="check")
    op.drop_constraint(op.f("ck_executions_valid_execution_status"), "executions", type_="check")

    op.add_column(
        "execution_operations",
        sa.Column("execution_plan_id", sa.String(length=255), nullable=True),
    )
    connection.execute(
        sa.text(
            "UPDATE execution_operations SET execution_plan_id = "
            "'legacy-operation-' || CAST(id AS VARCHAR)"
        )
    )
    op.alter_column("execution_operations", "execution_plan_id", nullable=False)
    op.drop_column("execution_operations", "metadata")
    op.drop_column("execution_operations", "source_content")
    op.drop_column("execution_operations", "operation_timeout_seconds")

    op.add_column(
        "execution_steps", sa.Column("execution_plan_id", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "execution_steps", sa.Column("plan_step_id", sa.String(length=255), nullable=True)
    )
    connection.execute(
        sa.text(
            "UPDATE execution_steps SET execution_plan_id = "
            "'legacy-execution-' || CAST(execution_id AS VARCHAR), plan_step_id = "
            "'legacy-step-' || CAST(id AS VARCHAR)"
        )
    )
    op.alter_column("execution_steps", "execution_plan_id", nullable=False)
    op.alter_column("execution_steps", "plan_step_id", nullable=False)
    op.create_index(op.f("ix_execution_steps_plan_step_id"), "execution_steps", ["plan_step_id"])
    op.drop_column("execution_steps", "step_timeout_seconds")

    op.add_column(
        "executions", sa.Column("execution_plan_id", sa.String(length=255), nullable=True)
    )
    connection.execute(
        sa.text(
            "UPDATE executions SET execution_plan_id = 'legacy-execution-' || CAST(id AS VARCHAR)"
        )
    )
    op.alter_column("executions", "execution_plan_id", nullable=False)
    connection.execute(
        sa.text("UPDATE executions SET project_id = 'unscoped' WHERE project_id IS NULL")
    )
    connection.execute(
        sa.text("UPDATE executions SET session_id = 'unscoped' WHERE session_id IS NULL")
    )
    op.alter_column("executions", "project_id", nullable=False)
    op.alter_column("executions", "session_id", nullable=False)
    op.drop_index(op.f("ix_executions_operation_wait_expires_at"), table_name="executions")
    op.alter_column(
        "executions", "operation_wait_expires_at", new_column_name="dynamic_wait_expires_at"
    )
    op.create_index(
        op.f("ix_executions_dynamic_wait_expires_at"),
        "executions",
        ["dynamic_wait_expires_at"],
    )
    op.alter_column(
        "executions", "finalization_requested", new_column_name="dynamic_finish_requested"
    )
    op.drop_column("executions", "operation_wait_timeout_seconds")
    op.alter_column("executions", "operation_mode", new_column_name="mode")

    connection.execute(
        sa.text(
            "UPDATE executions SET mode = CASE mode "
            "WHEN 'SINGLE' THEN 'STATIC' WHEN 'MULTI' THEN 'DYNAMIC' ELSE mode END"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE executions SET status = 'WAITING_FOR_CONTINUE' "
            "WHERE status = 'WAITING_FOR_OPERATION'"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE executions SET failure_type = 'DYNAMIC_WAIT_TIMEOUT' "
            "WHERE failure_type = 'OPERATION_WAIT_TIMEOUT'"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE execution_attempts SET failure_type = 'DYNAMIC_WAIT_TIMEOUT' "
            "WHERE failure_type = 'OPERATION_WAIT_TIMEOUT'"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE executions SET failure_type = 'INTERNAL_ERROR' "
            "WHERE failure_type IN ('OPERATION_TIMEOUT', 'STEP_TIMEOUT')"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE execution_attempts SET failure_type = 'INTERNAL_ERROR' "
            "WHERE failure_type IN ('OPERATION_TIMEOUT', 'STEP_TIMEOUT')"
        )
    )

    op.create_check_constraint(
        op.f("ck_executions_valid_execution_status"),
        "executions",
        "status IN ('QUEUED', 'DISPATCHED', 'RUNNING', 'WAITING_FOR_CONTINUE', "
        "'CANCEL_REQUESTED', 'CANCELLED', 'SUCCEEDED', 'FAILED')",
    )
    op.create_check_constraint(
        op.f("ck_executions_valid_execution_mode"),
        "executions",
        "mode IN ('STATIC', 'DYNAMIC')",
    )
    legacy_failure_values = (
        "'TOOL_ERROR', 'INFRASTRUCTURE_ERROR', 'WORKER_SHUTDOWN', "
        "'RUNTIME_UNAVAILABLE', 'LEASE_EXPIRED', 'INTERNAL_ERROR', "
        "'DYNAMIC_WAIT_TIMEOUT', 'EXECUTION_TIMEOUT', 'RUNTIME_SESSION_LOST'"
    )
    op.create_check_constraint(
        op.f("ck_executions_valid_failure_type"),
        "executions",
        f"failure_type IS NULL OR failure_type IN ({legacy_failure_values})",
    )
    op.create_check_constraint(
        op.f("ck_execution_attempts_valid_attempt_failure_type"),
        "execution_attempts",
        f"failure_type IS NULL OR failure_type IN ({legacy_failure_values})",
    )
