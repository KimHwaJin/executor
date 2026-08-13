"""add execution operations and dynamic continuation state

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-13
"""

from collections.abc import Sequence
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "execution_operations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("operation_number", sa.Integer(), nullable=False),
        sa.Column("first_sequence", sa.Integer(), nullable=False),
        sa.Column("last_sequence", sa.Integer(), nullable=False),
        sa.Column("execution_plan_id", sa.String(length=255), nullable=False),
        sa.Column("code_source_type", sa.String(length=32), nullable=False),
        sa.Column("code_path", sa.Text(), nullable=True),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("execution_attempt_id", sa.Uuid(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_by_type", sa.String(length=32), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("updated_by_type", sa.String(length=32), nullable=True),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("operation_number > 0", name="positive_operation_number"),
        sa.CheckConstraint("first_sequence >= 0", name="non_negative_first_sequence"),
        sa.CheckConstraint(
            "last_sequence >= first_sequence", name="valid_operation_sequence_range"
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name="valid_operation_status",
        ),
        sa.ForeignKeyConstraint(["execution_id"], ["executions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["execution_attempt_id"], ["execution_attempts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "execution_id", "operation_number", name="uq_operations_execution_number"
        ),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_execution_operations_execution_number",
        "execution_operations",
        ["execution_id", "operation_number"],
    )
    op.add_column("executions", sa.Column("active_operation_id", sa.Uuid(), nullable=True))
    op.create_index("ix_executions_active_operation_id", "executions", ["active_operation_id"])
    op.add_column("execution_steps", sa.Column("operation_id", sa.Uuid(), nullable=True))

    connection = op.get_bind()
    executions = connection.execute(
        sa.text(
            "SELECT id, execution_plan_id, code_source_type, code_path, source_sha256, "
            "idempotency_key, request_fingerprint, created_by_type, created_by, updated_by_type, "
            "updated_by, created_at, updated_at FROM executions"
        )
    ).mappings()
    for execution in executions:
        sequences = (
            connection.execute(
                sa.text(
                    "SELECT MIN(sequence) AS first_sequence, MAX(sequence) AS last_sequence "
                    "FROM execution_steps WHERE execution_id = :execution_id"
                ),
                {"execution_id": execution["id"]},
            )
            .mappings()
            .one()
        )
        if sequences["first_sequence"] is None:
            continue
        operation_id = uuid4()
        connection.execute(
            sa.text(
                "INSERT INTO execution_operations "
                "(id, execution_id, operation_number, first_sequence, last_sequence, "
                "execution_plan_id, code_source_type, code_path, source_sha256, idempotency_key, "
                "request_fingerprint, status, "
                "created_by_type, created_by, updated_by_type, updated_by, created_at, updated_at) "
                "VALUES (:id, :execution_id, 1, :first_sequence, :last_sequence, "
                ":execution_plan_id, :code_source_type, :code_path, :source_sha256, "
                ":idempotency_key, :request_fingerprint, 'QUEUED', "
                ":created_by_type, :created_by, :updated_by_type, :updated_by, :created_at, :updated_at)"
            ),
            {
                **dict(execution),
                **dict(sequences),
                "id": operation_id,
                "execution_id": execution["id"],
            },
        )
        connection.execute(
            sa.text(
                "UPDATE executions SET active_operation_id = :operation_id WHERE id = :execution_id"
            ),
            {"operation_id": operation_id, "execution_id": execution["id"]},
        )
        connection.execute(
            sa.text(
                "UPDATE execution_steps SET operation_id = :operation_id "
                "WHERE execution_id = :execution_id"
            ),
            {"operation_id": operation_id, "execution_id": execution["id"]},
        )

    op.alter_column("execution_steps", "operation_id", nullable=False)
    op.create_foreign_key(
        "fk_execution_steps_operation_id_execution_operations",
        "execution_steps",
        "execution_operations",
        ["operation_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_execution_steps_operation_id", "execution_steps", ["operation_id"])


def downgrade() -> None:
    op.drop_index("ix_execution_steps_operation_id", table_name="execution_steps")
    op.drop_constraint(
        "fk_execution_steps_operation_id_execution_operations",
        "execution_steps",
        type_="foreignkey",
    )
    op.drop_column("execution_steps", "operation_id")
    op.drop_index("ix_executions_active_operation_id", table_name="executions")
    op.drop_column("executions", "active_operation_id")
    op.drop_index("ix_execution_operations_execution_number", table_name="execution_operations")
    op.drop_table("execution_operations")
