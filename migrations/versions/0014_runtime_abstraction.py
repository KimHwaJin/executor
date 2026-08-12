"""Generalize Jupyter persistence into Runtime Targets and sessions.

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Constraints containing old enum values must be removed before data conversion.
    op.drop_constraint(
        op.f("ck_executions_valid_failure_type"), "executions", type_="check"
    )
    op.drop_constraint(
        op.f("ck_execution_attempts_valid_attempt_failure_type"),
        "execution_attempts",
        type_="check",
    )

    op.rename_table("jupyter_servers", "runtime_targets")
    op.rename_table("jupyter_server_purges", "runtime_target_purges")

    _rename_column("executions", "jupyter_pool", "runtime_pool")
    _rename_column("executions", "kernel_name", "runtime_profile")
    _rename_column("executions", "jupyter_server_id", "runtime_target_id")
    _rename_column("executions", "kernel_id", "runtime_session_id")
    _rename_column("executions", "retained_kernel_until", "retained_runtime_session_until")
    _rename_column("executions", "kernel_cleanup_status", "runtime_session_cleanup_status")
    _rename_column("execution_attempts", "jupyter_server_id", "runtime_target_id")
    _rename_column("execution_attempts", "kernel_id", "runtime_session_id")
    _rename_column(
        "execution_attempts",
        "kernel_cleanup_status",
        "runtime_session_cleanup_status",
    )
    _rename_column("runtime_targets", "supported_kernels", "supported_profiles")
    _rename_column("runtime_targets", "active_kernel_count", "active_session_count")
    _rename_column("runtime_target_purges", "server_id", "target_id")
    _rename_column("runtime_target_purges", "server_name", "target_name")

    op.add_column(
        "executions",
        sa.Column("runtime_type", sa.String(length=32), nullable=False, server_default="JUPYTER"),
    )
    op.alter_column("executions", "runtime_type", server_default=None)
    op.add_column(
        "runtime_targets",
        sa.Column("runtime_type", sa.String(length=32), nullable=False, server_default="JUPYTER"),
    )
    op.add_column("runtime_targets", sa.Column("connection_config", sa.JSON(), nullable=True))
    op.execute(
        "UPDATE runtime_targets SET connection_config = json_build_object('endpoint', endpoint)"
    )
    op.alter_column("runtime_targets", "connection_config", nullable=False)
    op.alter_column("runtime_targets", "runtime_type", server_default=None)
    op.drop_column("runtime_targets", "endpoint")

    op.add_column(
        "runtime_target_purges",
        sa.Column("runtime_type", sa.String(length=32), nullable=False, server_default="JUPYTER"),
    )
    op.add_column("runtime_target_purges", sa.Column("connection_config", sa.JSON(), nullable=True))
    op.execute(
        "UPDATE runtime_target_purges SET connection_config = "
        "json_build_object('endpoint', endpoint)"
    )
    op.alter_column("runtime_target_purges", "connection_config", nullable=False)
    op.alter_column("runtime_target_purges", "runtime_type", server_default=None)
    op.drop_column("runtime_target_purges", "endpoint")

    op.execute(
        "UPDATE executions SET failure_type = 'RUNTIME_UNAVAILABLE' "
        "WHERE failure_type = 'JUPYTER_UNAVAILABLE'"
    )
    op.execute(
        "UPDATE executions SET failure_type = 'RUNTIME_SESSION_LOST' "
        "WHERE failure_type = 'KERNEL_LOST'"
    )
    op.execute(
        "UPDATE execution_attempts SET failure_type = 'RUNTIME_UNAVAILABLE' "
        "WHERE failure_type = 'JUPYTER_UNAVAILABLE'"
    )
    op.execute(
        "UPDATE execution_attempts SET failure_type = 'RUNTIME_SESSION_LOST' "
        "WHERE failure_type = 'KERNEL_LOST'"
    )
    op.execute(
        "UPDATE command_receipts SET "
        "command_type = replace(command_type, 'jupyter_server.', 'runtime_target.'), "
        "result = ((result::jsonb - 'server_id') || "
        "jsonb_build_object('target_id', result::jsonb -> 'server_id'))::json "
        "WHERE command_type LIKE 'jupyter_server.%'"
    )

    _rename_constraints_and_indexes_up()
    _create_runtime_checks()


def downgrade() -> None:
    _drop_runtime_checks()
    op.drop_constraint(
        op.f("ck_execution_attempts_valid_attempt_failure_type"),
        "execution_attempts",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_executions_valid_failure_type"), "executions", type_="check"
    )

    op.add_column("runtime_targets", sa.Column("endpoint", sa.Text(), nullable=True))
    op.execute("UPDATE runtime_targets SET endpoint = connection_config ->> 'endpoint'")
    op.alter_column("runtime_targets", "endpoint", nullable=False)
    op.drop_column("runtime_targets", "connection_config")
    op.drop_column("runtime_targets", "runtime_type")

    op.add_column("runtime_target_purges", sa.Column("endpoint", sa.Text(), nullable=True))
    op.execute("UPDATE runtime_target_purges SET endpoint = connection_config ->> 'endpoint'")
    op.alter_column("runtime_target_purges", "endpoint", nullable=False)
    op.drop_column("runtime_target_purges", "connection_config")
    op.drop_column("runtime_target_purges", "runtime_type")
    op.drop_column("executions", "runtime_type")

    op.execute(
        "UPDATE executions SET failure_type = 'JUPYTER_UNAVAILABLE' "
        "WHERE failure_type = 'RUNTIME_UNAVAILABLE'"
    )
    op.execute(
        "UPDATE executions SET failure_type = 'KERNEL_LOST' "
        "WHERE failure_type = 'RUNTIME_SESSION_LOST'"
    )
    op.execute(
        "UPDATE execution_attempts SET failure_type = 'JUPYTER_UNAVAILABLE' "
        "WHERE failure_type = 'RUNTIME_UNAVAILABLE'"
    )
    op.execute(
        "UPDATE execution_attempts SET failure_type = 'KERNEL_LOST' "
        "WHERE failure_type = 'RUNTIME_SESSION_LOST'"
    )
    op.execute(
        "UPDATE command_receipts SET "
        "command_type = replace(command_type, 'runtime_target.', 'jupyter_server.'), "
        "result = ((result::jsonb - 'target_id') || "
        "jsonb_build_object('server_id', result::jsonb -> 'target_id'))::json "
        "WHERE command_type LIKE 'runtime_target.%'"
    )

    _rename_constraints_and_indexes_down()

    _rename_column("runtime_target_purges", "target_id", "server_id")
    _rename_column("runtime_target_purges", "target_name", "server_name")
    _rename_column("runtime_targets", "supported_profiles", "supported_kernels")
    _rename_column("runtime_targets", "active_session_count", "active_kernel_count")
    _rename_column("execution_attempts", "runtime_target_id", "jupyter_server_id")
    _rename_column("execution_attempts", "runtime_session_id", "kernel_id")
    _rename_column(
        "execution_attempts",
        "runtime_session_cleanup_status",
        "kernel_cleanup_status",
    )
    _rename_column("executions", "runtime_pool", "jupyter_pool")
    _rename_column("executions", "runtime_profile", "kernel_name")
    _rename_column("executions", "runtime_target_id", "jupyter_server_id")
    _rename_column("executions", "runtime_session_id", "kernel_id")
    _rename_column("executions", "retained_runtime_session_until", "retained_kernel_until")
    _rename_column("executions", "runtime_session_cleanup_status", "kernel_cleanup_status")
    op.rename_table("runtime_target_purges", "jupyter_server_purges")
    op.rename_table("runtime_targets", "jupyter_servers")
    _create_jupyter_checks()


def _rename_column(table: str, old: str, new: str) -> None:
    op.alter_column(table, old, new_column_name=new)


def _rename_constraint(table: str, old: str, new: str) -> None:
    op.execute(f'ALTER TABLE "{table}" RENAME CONSTRAINT "{old}" TO "{new}"')


def _rename_index(old: str, new: str) -> None:
    op.execute(f'ALTER INDEX "{old}" RENAME TO "{new}"')


def _rename_constraints_and_indexes_up() -> None:
    target_pairs = {
        "pk_jupyter_servers": "pk_runtime_targets",
        "uq_jupyter_servers_name": "uq_runtime_targets_name",
        "ck_jupyter_servers_complete_created_by": "ck_runtime_targets_complete_created_by",
        "ck_jupyter_servers_complete_updated_by": "ck_runtime_targets_complete_updated_by",
        "ck_jupyter_servers_positive_max_concurrency": "ck_runtime_targets_positive_max_concurrency",
        "ck_jupyter_servers_valid_created_by_type": "ck_runtime_targets_valid_created_by_type",
        "ck_jupyter_servers_valid_jupyter_server_status": "ck_runtime_targets_valid_runtime_target_status",
        "ck_jupyter_servers_valid_updated_by_type": "ck_runtime_targets_valid_updated_by_type",
    }
    for old, new in target_pairs.items():
        _rename_constraint("runtime_targets", old, new)
    purge_pairs = {
        "pk_jupyter_server_purges": "pk_runtime_target_purges",
        "uq_jupyter_server_purges_server_id": "uq_runtime_target_purges_target_id",
        "uq_jupyter_server_purges_idempotency_key": "uq_runtime_target_purges_idempotency_key",
        "ck_jupyter_server_purges_complete_created_by": "ck_runtime_target_purges_complete_created_by",
        "ck_jupyter_server_purges_complete_updated_by": "ck_runtime_target_purges_complete_updated_by",
        "ck_jupyter_server_purges_valid_created_by_type": "ck_runtime_target_purges_valid_created_by_type",
        "ck_jupyter_server_purges_valid_pool": "ck_runtime_target_purges_valid_pool",
        "ck_jupyter_server_purges_valid_updated_by_type": "ck_runtime_target_purges_valid_updated_by_type",
    }
    for old, new in purge_pairs.items():
        _rename_constraint("runtime_target_purges", old, new)
    _rename_constraint(
        "executions",
        "fk_executions_jupyter_server_id_jupyter_servers",
        "fk_executions_runtime_target_id_runtime_targets",
    )
    _rename_constraint(
        "execution_attempts",
        "fk_execution_attempts_jupyter_server_id_jupyter_servers",
        "fk_execution_attempts_runtime_target_id_runtime_targets",
    )
    _rename_constraint(
        "executions",
        "ck_executions_valid_jupyter_pool",
        "ck_executions_valid_runtime_pool",
    )
    _rename_constraint(
        "executions",
        "ck_executions_valid_kernel_cleanup_status",
        "ck_executions_valid_runtime_session_cleanup_status",
    )
    _rename_constraint(
        "execution_attempts",
        "ck_execution_attempts_valid_attempt_kernel_cleanup_status",
        "ck_execution_attempts_valid_runtime_session_cleanup_status",
    )
    _rename_index("ix_jupyter_servers_created_cursor", "ix_runtime_targets_created_cursor")
    _rename_index("ix_jupyter_servers_pool_status", "ix_runtime_targets_pool_status")
    _rename_index("ix_executions_jupyter_server_id", "ix_executions_runtime_target_id")
    _rename_index("ix_execution_attempts_server_status", "ix_execution_attempts_target_status")


def _rename_constraints_and_indexes_down() -> None:
    # Reverse names before tables/columns are renamed back.
    pairs = {
        "pk_runtime_targets": "pk_jupyter_servers",
        "uq_runtime_targets_name": "uq_jupyter_servers_name",
        "ck_runtime_targets_complete_created_by": "ck_jupyter_servers_complete_created_by",
        "ck_runtime_targets_complete_updated_by": "ck_jupyter_servers_complete_updated_by",
        "ck_runtime_targets_positive_max_concurrency": "ck_jupyter_servers_positive_max_concurrency",
        "ck_runtime_targets_valid_created_by_type": "ck_jupyter_servers_valid_created_by_type",
        "ck_runtime_targets_valid_runtime_target_status": "ck_jupyter_servers_valid_jupyter_server_status",
        "ck_runtime_targets_valid_updated_by_type": "ck_jupyter_servers_valid_updated_by_type",
    }
    for old, new in pairs.items():
        _rename_constraint("runtime_targets", old, new)
    purge_pairs = {
        "pk_runtime_target_purges": "pk_jupyter_server_purges",
        "uq_runtime_target_purges_target_id": "uq_jupyter_server_purges_server_id",
        "uq_runtime_target_purges_idempotency_key": "uq_jupyter_server_purges_idempotency_key",
        "ck_runtime_target_purges_complete_created_by": "ck_jupyter_server_purges_complete_created_by",
        "ck_runtime_target_purges_complete_updated_by": "ck_jupyter_server_purges_complete_updated_by",
        "ck_runtime_target_purges_valid_created_by_type": "ck_jupyter_server_purges_valid_created_by_type",
        "ck_runtime_target_purges_valid_pool": "ck_jupyter_server_purges_valid_pool",
        "ck_runtime_target_purges_valid_updated_by_type": "ck_jupyter_server_purges_valid_updated_by_type",
    }
    for old, new in purge_pairs.items():
        _rename_constraint("runtime_target_purges", old, new)
    _rename_constraint(
        "executions",
        "fk_executions_runtime_target_id_runtime_targets",
        "fk_executions_jupyter_server_id_jupyter_servers",
    )
    _rename_constraint(
        "execution_attempts",
        "fk_execution_attempts_runtime_target_id_runtime_targets",
        "fk_execution_attempts_jupyter_server_id_jupyter_servers",
    )
    _rename_constraint(
        "executions",
        "ck_executions_valid_runtime_pool",
        "ck_executions_valid_jupyter_pool",
    )
    _rename_constraint(
        "executions",
        "ck_executions_valid_runtime_session_cleanup_status",
        "ck_executions_valid_kernel_cleanup_status",
    )
    _rename_constraint(
        "execution_attempts",
        "ck_execution_attempts_valid_runtime_session_cleanup_status",
        "ck_execution_attempts_valid_attempt_kernel_cleanup_status",
    )
    _rename_index("ix_runtime_targets_created_cursor", "ix_jupyter_servers_created_cursor")
    _rename_index("ix_runtime_targets_pool_status", "ix_jupyter_servers_pool_status")
    _rename_index("ix_executions_runtime_target_id", "ix_executions_jupyter_server_id")
    _rename_index("ix_execution_attempts_target_status", "ix_execution_attempts_server_status")


def _create_runtime_checks() -> None:
    op.create_check_constraint(
        op.f("ck_runtime_targets_valid_runtime_type"),
        "runtime_targets",
        "runtime_type IN ('JUPYTER')",
    )
    op.create_check_constraint(
        op.f("ck_runtime_target_purges_valid_runtime_type"),
        "runtime_target_purges",
        "runtime_type IN ('JUPYTER')",
    )
    op.create_check_constraint(
        op.f("ck_executions_valid_runtime_type"),
        "executions",
        "runtime_type IN ('JUPYTER')",
    )
    failure_values = (
        "'TOOL_ERROR', 'INFRASTRUCTURE_ERROR', 'WORKER_SHUTDOWN', "
        "'RUNTIME_UNAVAILABLE', 'LEASE_EXPIRED', 'INTERNAL_ERROR', "
        "'DYNAMIC_WAIT_TIMEOUT', 'EXECUTION_TIMEOUT', 'RUNTIME_SESSION_LOST'"
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


def _drop_runtime_checks() -> None:
    op.drop_constraint(
        op.f("ck_runtime_targets_valid_runtime_type"),
        "runtime_targets",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_runtime_target_purges_valid_runtime_type"),
        "runtime_target_purges",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_executions_valid_runtime_type"), "executions", type_="check"
    )


def _create_jupyter_checks() -> None:
    old_failure_values = (
        "'TOOL_ERROR', 'INFRASTRUCTURE_ERROR', 'WORKER_SHUTDOWN', "
        "'JUPYTER_UNAVAILABLE', 'LEASE_EXPIRED', 'INTERNAL_ERROR', "
        "'DYNAMIC_WAIT_TIMEOUT', 'EXECUTION_TIMEOUT', 'KERNEL_LOST'"
    )
    op.create_check_constraint(
        op.f("ck_executions_valid_failure_type"),
        "executions",
        f"failure_type IS NULL OR failure_type IN ({old_failure_values})",
    )
    op.create_check_constraint(
        op.f("ck_execution_attempts_valid_attempt_failure_type"),
        "execution_attempts",
        f"failure_type IS NULL OR failure_type IN ({old_failure_values})",
    )
