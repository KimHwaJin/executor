"""Record what ended an Execution and why a MULTI Operation failed.

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

_FAILURE_TYPES_BEFORE = (
    "'TOOL_ERROR', 'INFRASTRUCTURE_ERROR', 'WORKER_SHUTDOWN', "
    "'RUNTIME_UNAVAILABLE', 'LEASE_EXPIRED', 'INTERNAL_ERROR', "
    "'OPERATION_WAIT_TIMEOUT', 'OPERATION_TIMEOUT', 'STEP_TIMEOUT', "
    "'EXECUTION_TIMEOUT', 'OUTPUT_LIMIT_EXCEEDED', 'RUNTIME_SESSION_LOST', "
    "'COMPLETION_FAILED'"
)
_FAILURE_TYPES = (
    "'TOOL_ERROR', 'INFRASTRUCTURE_ERROR', 'WORKER_SHUTDOWN', "
    "'RUNTIME_UNAVAILABLE', 'LEASE_EXPIRED', 'INTERNAL_ERROR', "
    "'OPERATION_WAIT_TIMEOUT', 'OPERATION_TIMEOUT', 'STEP_TIMEOUT', "
    "'EXECUTION_TIMEOUT', 'QUEUE_TIMEOUT', 'OUTPUT_LIMIT_EXCEEDED', "
    "'RUNTIME_SESSION_LOST', 'COMPLETION_FAILED'"
)


def upgrade() -> None:
    op.add_column(
        "executions",
        sa.Column("finalized_by", sa.String(length=32), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_executions_valid_finalized_by"),
        "executions",
        "finalized_by IS NULL OR finalized_by IN "
        "('AGENT', 'WAIT_TIMEOUT', 'MAX_RUNTIME')",
    )
    op.drop_constraint(
        op.f("ck_executions_valid_failure_type"), "executions", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_executions_valid_failure_type"),
        "executions",
        f"failure_type IS NULL OR failure_type IN ({_FAILURE_TYPES})",
    )
    op.add_column(
        "execution_operations",
        sa.Column("failure_type", sa.String(length=32), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_execution_operations_valid_operation_failure_type"),
        "execution_operations",
        f"failure_type IS NULL OR failure_type IN ({_FAILURE_TYPES})",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_execution_operations_valid_operation_failure_type"),
        "execution_operations",
        type_="check",
    )
    op.drop_column("execution_operations", "failure_type")
    op.drop_constraint(
        op.f("ck_executions_valid_failure_type"), "executions", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_executions_valid_failure_type"),
        "executions",
        f"failure_type IS NULL OR failure_type IN ({_FAILURE_TYPES_BEFORE})",
    )
    op.drop_constraint(
        op.f("ck_executions_valid_finalized_by"), "executions", type_="check"
    )
    op.drop_column("executions", "finalized_by")
