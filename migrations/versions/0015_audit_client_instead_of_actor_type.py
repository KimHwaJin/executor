"""Record the calling client instead of a caller-declared actor type.

Callers no longer send ``actor`` in request bodies. Audit columns now hold
the user from ``X-User-Id`` (``*_by``) and the API key's client name
(``*_by_client``). Existing rows keep their ``*_by`` value and get no client.

Dropping ``*_by_type`` breaks Pods still on 0014, so this release deploys
with drain and ``Recreate``.

Revision ID: 0015
Revises: 0014
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

_TABLES = (
    "executions",
    "executor_maintenance",
    "maintenance_runs",
    "maintenance_run_targets",
    "runtime_targets",
    "runtime_target_purges",
    "execution_attempts",
    "execution_diagnostics",
    "execution_events",
    "execution_operations",
    "execution_steps",
    "execution_step_attempts",
    "execution_artifacts",
    "outbox_events",
)
_ACTOR_TYPES = "'AGENT', 'USER', 'BATCH'"


def upgrade() -> None:
    for table in _TABLES:
        for name in (
            "valid_created_by_type",
            "valid_updated_by_type",
            "complete_created_by",
            "complete_updated_by",
        ):
            op.drop_constraint(
                op.f(f"ck_{table}_{name}"), table, type_="check"
            )
        for prefix in ("created", "updated"):
            op.drop_column(table, f"{prefix}_by_type")
            op.add_column(
                table,
                sa.Column(
                    f"{prefix}_by_client", sa.String(length=64), nullable=True
                ),
            )


def downgrade() -> None:
    for table in _TABLES:
        for prefix in ("created", "updated"):
            op.drop_column(table, f"{prefix}_by_client")
            op.add_column(
                table,
                sa.Column(
                    f"{prefix}_by_type", sa.String(length=32), nullable=True
                ),
            )
            # *_by now always names a user.
            op.execute(
                f"UPDATE {table} SET {prefix}_by_type = 'USER' "
                f"WHERE {prefix}_by IS NOT NULL"
            )
            op.create_check_constraint(
                op.f(f"ck_{table}_valid_{prefix}_by_type"),
                table,
                f"{prefix}_by_type IS NULL OR {prefix}_by_type IN "
                f"({_ACTOR_TYPES})",
            )
            op.create_check_constraint(
                op.f(f"ck_{table}_complete_{prefix}_by"),
                table,
                f"({prefix}_by_type IS NULL) = ({prefix}_by IS NULL)",
            )
