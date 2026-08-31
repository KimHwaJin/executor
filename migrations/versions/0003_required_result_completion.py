"""Allow non-retryable completion failures without resetting existing data.

Revision ID: 0003
Revises: 0002
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_OLD_VALUES = (
    "'TOOL_ERROR', 'INFRASTRUCTURE_ERROR', 'WORKER_SHUTDOWN', "
    "'RUNTIME_UNAVAILABLE', 'LEASE_EXPIRED', 'INTERNAL_ERROR', "
    "'OPERATION_WAIT_TIMEOUT', 'OPERATION_TIMEOUT', 'STEP_TIMEOUT', "
    "'EXECUTION_TIMEOUT', 'OUTPUT_LIMIT_EXCEEDED', 'RUNTIME_SESSION_LOST'"
)


def _replace_constraints(values: str) -> None:
    for table, name in (
        ("executions", "ck_executions_valid_failure_type"),
        (
            "execution_attempts",
            "ck_execution_attempts_valid_attempt_failure_type",
        ),
    ):
        op.drop_constraint(op.f(name), table, type_="check")
        op.create_check_constraint(
            op.f(name),
            table,
            f"failure_type IS NULL OR failure_type IN ({values})",
        )


def upgrade() -> None:
    _replace_constraints(f"{_OLD_VALUES}, 'COMPLETION_FAILED'")


def downgrade() -> None:
    # PostgreSQL validates existing rows. If COMPLETION_FAILED exists this
    # transaction rolls back; never delete or silently reclassify evidence.
    _replace_constraints(_OLD_VALUES)
