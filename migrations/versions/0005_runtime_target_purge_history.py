"""Retain Runtime identity after registration deletion.

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_REFERENCES = (
    ("executions", "fk_executions_runtime_target_id_runtime_targets"),
    (
        "execution_attempts",
        "fk_execution_attempts_runtime_target_id_runtime_targets",
    ),
)


def upgrade() -> None:
    # UUIDs become historical references to an active target OR a purge
    # tombstone. Never null them or cascade-delete execution evidence.
    for table, constraint in _REFERENCES:
        op.drop_constraint(constraint, table, type_="foreignkey")
    op.drop_constraint(
        "uq_runtime_target_purges_idempotency_key",
        "runtime_target_purges",
        type_="unique",
    )
    op.drop_column("runtime_target_purges", "idempotency_key")
    op.drop_column("runtime_target_purges", "request_fingerprint")


def downgrade() -> None:
    connection = op.get_bind()
    for table, _constraint in _REFERENCES:
        missing = connection.scalar(
            sa.text(
                f"SELECT EXISTS (SELECT 1 FROM {table} h "
                "WHERE h.runtime_target_id IS NOT NULL AND NOT EXISTS "
                "(SELECT 1 FROM runtime_targets t "
                "WHERE t.id = h.runtime_target_id))"
            )
        )
        if missing:
            raise RuntimeError(
                "Cannot downgrade: execution history references purged "
                "Runtime Targets. Keep 0005 or restore a pre-purge backup."
            )
    op.add_column(
        "runtime_target_purges",
        sa.Column("idempotency_key", sa.String(255), nullable=True),
    )
    op.add_column(
        "runtime_target_purges",
        sa.Column("request_fingerprint", sa.String(64), nullable=True),
    )
    # Old request keys/hashes cannot be recovered. Synthetic unique values
    # restore the old schema without impersonating an old caller request.
    connection.execute(
        sa.text(
            "UPDATE runtime_target_purges SET idempotency_key = "
            "'restored-purge-' || CAST(id AS TEXT), "
            "request_fingerprint = :fingerprint"
        ),
        {"fingerprint": "0" * 64},
    )
    for column, type_ in (
        ("idempotency_key", sa.String(255)),
        ("request_fingerprint", sa.String(64)),
    ):
        op.alter_column(
            "runtime_target_purges",
            column,
            nullable=False,
            existing_type=type_,
        )
    op.create_unique_constraint(
        "uq_runtime_target_purges_idempotency_key",
        "runtime_target_purges",
        ["idempotency_key"],
    )
    for table, constraint in _REFERENCES:
        op.create_foreign_key(
            constraint, table, "runtime_targets", ["runtime_target_id"], ["id"]
        )
