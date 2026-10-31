"""Scope idempotency keys to the tenant or Execution and drop DISPATCHED.

Submit keys become unique per tenant. Keys of commands on an existing
Execution (cancel, finalize, Operation, retry, Artifact) become unique per
Execution, so callers in different tenants never collide.

Revision ID: 0014
Revises: 0013
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

_STATUSES = (
    "'QUEUED', 'RUNNING', 'WAITING_FOR_OPERATION', 'FINALIZING', "
    "'CANCEL_REQUESTED', 'CANCELLED', 'SUCCEEDED', 'FAILED'"
)


def _status_check(values: str) -> None:
    op.drop_constraint(
        op.f("ck_executions_valid_execution_status"),
        "executions",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_executions_valid_execution_status"),
        "executions",
        f"status IN ({values})",
    )


def upgrade() -> None:
    _status_check(_STATUSES)

    op.drop_constraint(
        op.f("uq_executions_idempotency_key"), "executions", type_="unique"
    )
    op.create_unique_constraint(
        "uq_executions_tenant_idempotency_key",
        "executions",
        ["tenant_id", "idempotency_key"],
    )
    op.drop_constraint(
        op.f("uq_executions_cancel_idempotency_key"),
        "executions",
        type_="unique",
    )
    for table in ("execution_operations", "execution_retries"):
        op.drop_constraint(
            op.f(f"uq_{table}_idempotency_key"), table, type_="unique"
        )
        op.create_unique_constraint(
            f"uq_{table}_execution_idempotency_key",
            table,
            ["execution_id", "idempotency_key"],
        )

    op.add_column(
        "command_receipts",
        sa.Column(
            "scope", sa.String(length=64), nullable=False, server_default=""
        ),
    )
    # Receipts of commands on an Execution carry its id in the result.
    # Artifact materialization receipts do not and stay unscoped; the
    # release starts from an empty database (see 0012).
    op.execute(
        "UPDATE command_receipts SET scope = 'execution:' || "
        "(result::json ->> 'execution_id') "
        "WHERE command_type IN "
        "('execution_finalize', 'execution_operation_create') "
        "AND (result::json ->> 'execution_id') IS NOT NULL"
    )
    op.drop_constraint(
        op.f("uq_command_receipts_idempotency_key"),
        "command_receipts",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_command_receipts_scope_idempotency_key",
        "command_receipts",
        ["scope", "idempotency_key"],
    )


def downgrade() -> None:
    # Fails instead of guessing if two scopes now share a key.
    op.drop_constraint(
        "uq_command_receipts_scope_idempotency_key",
        "command_receipts",
        type_="unique",
    )
    op.create_unique_constraint(
        op.f("uq_command_receipts_idempotency_key"),
        "command_receipts",
        ["idempotency_key"],
    )
    op.drop_column("command_receipts", "scope")
    for table in ("execution_retries", "execution_operations"):
        op.drop_constraint(
            f"uq_{table}_execution_idempotency_key", table, type_="unique"
        )
        op.create_unique_constraint(
            op.f(f"uq_{table}_idempotency_key"), table, ["idempotency_key"]
        )
    op.create_unique_constraint(
        op.f("uq_executions_cancel_idempotency_key"),
        "executions",
        ["cancel_idempotency_key"],
    )
    op.drop_constraint(
        "uq_executions_tenant_idempotency_key", "executions", type_="unique"
    )
    op.create_unique_constraint(
        op.f("uq_executions_idempotency_key"),
        "executions",
        ["idempotency_key"],
    )
    _status_check("'DISPATCHED', " + _STATUSES)
