"""Add Jupyter server registry, execution attempts, and worker state.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "jupyter_servers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("credential_ref", sa.String(length=255), nullable=False),
        sa.Column(
            "pool",
            sa.Enum(
                "INTERACTIVE",
                "BATCH",
                name="jupyter_server_pool",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "ACTIVE",
                "DRAINING",
                "OFFLINE",
                name="jupyter_server_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("max_concurrent_executions", sa.Integer(), nullable=False),
        sa.Column("supported_kernels", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'DRAINING', 'OFFLINE')",
            name=op.f("ck_jupyter_servers_valid_jupyter_server_status"),
        ),
        sa.CheckConstraint(
            "max_concurrent_executions > 0",
            name=op.f("ck_jupyter_servers_positive_max_concurrency"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jupyter_servers")),
        sa.UniqueConstraint("name", name=op.f("uq_jupyter_servers_name")),
    )
    op.create_index(
        "ix_jupyter_servers_pool_status", "jupyter_servers", ["pool", "status"], unique=False
    )

    op.add_column("executions", sa.Column("jupyter_server_id", sa.Uuid(), nullable=True))
    op.add_column("executions", sa.Column("kernel_id", sa.String(length=255), nullable=True))
    op.add_column("executions", sa.Column("workspace_path", sa.Text(), nullable=True))
    op.add_column("executions", sa.Column("notebook_path", sa.Text(), nullable=True))
    op.add_column("executions", sa.Column("error_message", sa.Text(), nullable=True))
    op.add_column("executions", sa.Column("lease_owner", sa.String(length=255), nullable=True))
    op.add_column(
        "executions", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "executions", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_foreign_key(
        op.f("fk_executions_jupyter_server_id_jupyter_servers"),
        "executions",
        "jupyter_servers",
        ["jupyter_server_id"],
        ["id"],
    )
    op.create_index(
        op.f("ix_executions_jupyter_server_id"),
        "executions",
        ["jupyter_server_id"],
        unique=False,
    )
    op.create_index(
        "ix_executions_lease", "executions", ["status", "lease_expires_at"], unique=False
    )

    op.add_column(
        "execution_steps",
        sa.Column("outputs", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
    )
    op.add_column("execution_steps", sa.Column("error_message", sa.Text(), nullable=True))
    op.add_column(
        "execution_steps", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "execution_steps", sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.alter_column("execution_steps", "outputs", server_default=None)

    op.create_table(
        "execution_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("jupyter_server_id", sa.Uuid(), nullable=False),
        sa.Column("kernel_id", sa.String(length=255), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "RUNNING",
                "SUCCEEDED",
                "FAILED",
                "CANCELLED",
                name="attempt_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("lease_owner", sa.String(length=255), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "attempt_number > 0", name=op.f("ck_execution_attempts_positive_attempt_number")
        ),
        sa.CheckConstraint(
            "status IN ('RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name=op.f("ck_execution_attempts_valid_attempt_status"),
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["executions.id"],
            name=op.f("fk_execution_attempts_execution_id_executions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["jupyter_server_id"],
            ["jupyter_servers.id"],
            name=op.f("fk_execution_attempts_jupyter_server_id_jupyter_servers"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_execution_attempts")),
        sa.UniqueConstraint(
            "execution_id",
            "attempt_number",
            name=op.f("uq_execution_attempts_execution_attempt"),
        ),
    )
    op.create_index(
        "ix_execution_attempts_lease",
        "execution_attempts",
        ["status", "lease_expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_execution_attempts_server_status",
        "execution_attempts",
        ["jupyter_server_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_execution_attempts_server_status", table_name="execution_attempts")
    op.drop_index("ix_execution_attempts_lease", table_name="execution_attempts")
    op.drop_table("execution_attempts")
    op.drop_column("execution_steps", "finished_at")
    op.drop_column("execution_steps", "started_at")
    op.drop_column("execution_steps", "error_message")
    op.drop_column("execution_steps", "outputs")
    op.drop_index("ix_executions_lease", table_name="executions")
    op.drop_index(op.f("ix_executions_jupyter_server_id"), table_name="executions")
    op.drop_constraint(
        op.f("fk_executions_jupyter_server_id_jupyter_servers"),
        "executions",
        type_="foreignkey",
    )
    op.drop_column("executions", "heartbeat_at")
    op.drop_column("executions", "lease_expires_at")
    op.drop_column("executions", "lease_owner")
    op.drop_column("executions", "error_message")
    op.drop_column("executions", "notebook_path")
    op.drop_column("executions", "workspace_path")
    op.drop_column("executions", "kernel_id")
    op.drop_column("executions", "jupyter_server_id")
    op.drop_index("ix_jupyter_servers_pool_status", table_name="jupyter_servers")
    op.drop_table("jupyter_servers")
