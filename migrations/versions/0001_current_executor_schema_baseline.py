"""Create the complete Executor schema baseline.

Revision ID: 0001
Revises:
Create Date: 2026-08-19 07:21:16.393317

This pre-release baseline replaces the discarded incremental development chain and must be
applied to an empty database.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "command_receipts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("command_type", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_command_receipts")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_command_receipts_idempotency_key")),
    )
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("aggregate_type", sa.String(length=128), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=255), nullable=False),
        sa.Column(
            "destination",
            sa.Enum("WORK", "EVENTS", name="outbox_destination", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_by_type",
            sa.Enum("AGENT", "USER", "BATCH", name="actor_type", native_enum=False, length=32),
            nullable=True,
        ),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column(
            "updated_by_type",
            sa.Enum("AGENT", "USER", "BATCH", name="actor_type", native_enum=False, length=32),
            nullable=True,
        ),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        sa.Column("traceparent", sa.String(length=512), nullable=True),
        sa.Column("tracestate", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("PENDING", "PUBLISHED", name="outbox_status", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.CheckConstraint(
            "created_by_type IS NULL OR created_by_type IN ('AGENT', 'USER', 'BATCH')",
            name=op.f("ck_outbox_events_valid_created_by_type"),
        ),
        sa.CheckConstraint(
            "destination IN ('WORK', 'EVENTS')",
            name=op.f("ck_outbox_events_valid_outbox_destination"),
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'PUBLISHED')", name=op.f("ck_outbox_events_valid_outbox_status")
        ),
        sa.CheckConstraint(
            "updated_by_type IS NULL OR updated_by_type IN ('AGENT', 'USER', 'BATCH')",
            name=op.f("ck_outbox_events_valid_updated_by_type"),
        ),
        sa.CheckConstraint(
            "(created_by_type IS NULL) = (created_by IS NULL)",
            name=op.f("ck_outbox_events_complete_created_by"),
        ),
        sa.CheckConstraint(
            "(updated_by_type IS NULL) = (updated_by IS NULL)",
            name=op.f("ck_outbox_events_complete_updated_by"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox_events")),
    )
    op.create_index(
        op.f("ix_outbox_events_aggregate_id"), "outbox_events", ["aggregate_id"], unique=False
    )
    op.create_index(
        "ix_outbox_execution_cursor",
        "outbox_events",
        ["aggregate_type", "aggregate_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_outbox_pending", "outbox_events", ["status", "available_at", "created_at"], unique=False
    )
    op.create_table(
        "runtime_target_purges",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("target_name", sa.String(length=255), nullable=False),
        sa.Column(
            "runtime_type",
            sa.Enum("JUPYTER", name="runtime_target_purge_type", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("connection_config", sa.JSON(), nullable=False),
        sa.Column(
            "pool",
            sa.Enum(
                "INTERACTIVE",
                "BATCH",
                name="runtime_target_purge_pool",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "created_by_type",
            sa.Enum("AGENT", "USER", "BATCH", name="actor_type", native_enum=False, length=32),
            nullable=True,
        ),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column(
            "updated_by_type",
            sa.Enum("AGENT", "USER", "BATCH", name="actor_type", native_enum=False, length=32),
            nullable=True,
        ),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "created_by_type IS NULL OR created_by_type IN ('AGENT', 'USER', 'BATCH')",
            name=op.f("ck_runtime_target_purges_valid_created_by_type"),
        ),
        sa.CheckConstraint(
            "pool IN ('INTERACTIVE', 'BATCH')", name=op.f("ck_runtime_target_purges_valid_pool")
        ),
        sa.CheckConstraint(
            "runtime_type IN ('JUPYTER')", name=op.f("ck_runtime_target_purges_valid_runtime_type")
        ),
        sa.CheckConstraint(
            "updated_by_type IS NULL OR updated_by_type IN ('AGENT', 'USER', 'BATCH')",
            name=op.f("ck_runtime_target_purges_valid_updated_by_type"),
        ),
        sa.CheckConstraint(
            "(created_by_type IS NULL) = (created_by IS NULL)",
            name=op.f("ck_runtime_target_purges_complete_created_by"),
        ),
        sa.CheckConstraint(
            "(updated_by_type IS NULL) = (updated_by IS NULL)",
            name=op.f("ck_runtime_target_purges_complete_updated_by"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runtime_target_purges")),
        sa.UniqueConstraint(
            "idempotency_key", name=op.f("uq_runtime_target_purges_idempotency_key")
        ),
        sa.UniqueConstraint("target_id", name=op.f("uq_runtime_target_purges_target_id")),
    )
    op.create_table(
        "runtime_targets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "runtime_type",
            sa.Enum("JUPYTER", name="runtime_target_type", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("connection_config", sa.JSON(), nullable=False),
        sa.Column("credential_ref", sa.String(length=255), nullable=False),
        sa.Column("credential_ciphertext", sa.Text(), nullable=True),
        sa.Column(
            "pool",
            sa.Enum(
                "INTERACTIVE", "BATCH", name="runtime_target_pool", native_enum=False, length=32
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "ACTIVE",
                "DRAINING",
                "OFFLINE",
                name="runtime_target_status",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("max_concurrent_executions", sa.Integer(), nullable=False),
        sa.Column("supported_profiles", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_health_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_health_error", sa.String(length=500), nullable=True),
        sa.Column("active_session_count", sa.Integer(), nullable=True),
        sa.Column("resource_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resource_last_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resource_last_error", sa.String(length=500), nullable=True),
        sa.Column("resource_source", sa.String(length=64), nullable=True),
        sa.Column("resource_estimated", sa.Boolean(), nullable=True),
        sa.Column("resource_process_count", sa.Integer(), nullable=True),
        sa.Column("cpu_used_cores", sa.Float(), nullable=True),
        sa.Column("cpu_capacity_cores", sa.Float(), nullable=True),
        sa.Column("cpu_utilization", sa.Float(), nullable=True),
        sa.Column("memory_used_bytes", sa.BigInteger(), nullable=True),
        sa.Column("memory_capacity_bytes", sa.BigInteger(), nullable=True),
        sa.Column("memory_utilization", sa.Float(), nullable=True),
        sa.Column("resource_errors", sa.JSON(), nullable=False),
        sa.Column(
            "created_by_type",
            sa.Enum("AGENT", "USER", "BATCH", name="actor_type", native_enum=False, length=32),
            nullable=True,
        ),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column(
            "updated_by_type",
            sa.Enum("AGENT", "USER", "BATCH", name="actor_type", native_enum=False, length=32),
            nullable=True,
        ),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "created_by_type IS NULL OR created_by_type IN ('AGENT', 'USER', 'BATCH')",
            name=op.f("ck_runtime_targets_valid_created_by_type"),
        ),
        sa.CheckConstraint(
            "runtime_type IN ('JUPYTER')", name=op.f("ck_runtime_targets_valid_runtime_type")
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'DRAINING', 'OFFLINE')",
            name=op.f("ck_runtime_targets_valid_runtime_target_status"),
        ),
        sa.CheckConstraint(
            "updated_by_type IS NULL OR updated_by_type IN ('AGENT', 'USER', 'BATCH')",
            name=op.f("ck_runtime_targets_valid_updated_by_type"),
        ),
        sa.CheckConstraint(
            "(created_by_type IS NULL) = (created_by IS NULL)",
            name=op.f("ck_runtime_targets_complete_created_by"),
        ),
        sa.CheckConstraint(
            "(updated_by_type IS NULL) = (updated_by IS NULL)",
            name=op.f("ck_runtime_targets_complete_updated_by"),
        ),
        sa.CheckConstraint(
            "max_concurrent_executions > 0",
            name=op.f("ck_runtime_targets_positive_max_concurrency"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runtime_targets")),
        sa.UniqueConstraint("name", name=op.f("uq_runtime_targets_name")),
    )
    op.create_index(
        "ix_runtime_targets_created_cursor", "runtime_targets", ["created_at", "id"], unique=False
    )
    op.create_index(
        "ix_runtime_targets_pool_status",
        "runtime_targets",
        ["pool", "enabled", "status"],
        unique=False,
    )
    op.create_table(
        "executions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("cancel_idempotency_key", sa.String(length=255), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "QUEUED",
                "DISPATCHED",
                "RUNNING",
                "WAITING_FOR_OPERATION",
                "FINALIZING",
                "CANCEL_REQUESTED",
                "CANCELLED",
                "SUCCEEDED",
                "FAILED",
                name="execution_status",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "operation_mode",
            sa.Enum("SINGLE", "MULTI", name="operation_mode", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("operation_wait_timeout_seconds", sa.Integer(), nullable=True),
        sa.Column(
            "trigger_type",
            sa.Enum("INTERACTIVE", "BATCH", name="trigger_type", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column(
            "runtime_type",
            sa.Enum("JUPYTER", name="runtime_type", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column(
            "runtime_pool",
            sa.Enum("INTERACTIVE", "BATCH", name="runtime_pool", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("runtime_profile", sa.String(length=128), nullable=False),
        sa.Column(
            "code_source_type",
            sa.Enum("INLINE", "PATH", name="code_source_type", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("code_path", sa.Text(), nullable=True),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("project_id", sa.String(length=255), nullable=True),
        sa.Column("session_id", sa.String(length=255), nullable=True),
        sa.Column("task_id", sa.String(length=255), nullable=False),
        sa.Column("workflow_id", sa.String(length=255), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("cancellation_reason", sa.Text(), nullable=True),
        sa.Column("runtime_target_id", sa.Uuid(), nullable=True),
        sa.Column("runtime_session_id", sa.String(length=255), nullable=True),
        sa.Column("workspace_path", sa.Text(), nullable=True),
        sa.Column("notebook_path", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "failure_type",
            sa.Enum(
                "TOOL_ERROR",
                "INFRASTRUCTURE_ERROR",
                "WORKER_SHUTDOWN",
                "RUNTIME_UNAVAILABLE",
                "LEASE_EXPIRED",
                "INTERNAL_ERROR",
                "OPERATION_WAIT_TIMEOUT",
                "OPERATION_TIMEOUT",
                "STEP_TIMEOUT",
                "EXECUTION_TIMEOUT",
                "RUNTIME_SESSION_LOST",
                name="failure_type",
                native_enum=False,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column("lease_owner", sa.String(length=255), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "retry_strategy",
            sa.Enum(
                "NOT_RETRYABLE",
                "FROM_FAILED_STEP",
                "FROM_START",
                name="retry_strategy",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("retry_from_sequence", sa.Integer(), nullable=True),
        sa.Column("retained_runtime_session_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("recovery_count", sa.Integer(), nullable=False),
        sa.Column(
            "runtime_session_cleanup_status",
            sa.Enum(
                "NOT_REQUIRED",
                "PENDING",
                "SUCCEEDED",
                "FAILED",
                name="runtime_session_cleanup_status",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("finalization_requested", sa.Boolean(), nullable=False),
        sa.Column("active_operation_id", sa.Uuid(), nullable=True),
        sa.Column("operation_wait_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("execution_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("traceparent", sa.String(length=512), nullable=True),
        sa.Column("tracestate", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_by_type",
            sa.Enum("AGENT", "USER", "BATCH", name="actor_type", native_enum=False, length=32),
            nullable=True,
        ),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column(
            "updated_by_type",
            sa.Enum("AGENT", "USER", "BATCH", name="actor_type", native_enum=False, length=32),
            nullable=True,
        ),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(code_source_type = 'INLINE' AND code_path IS NULL) OR (code_source_type = 'PATH' AND code_path IS NOT NULL)",
            name=op.f("ck_executions_valid_code_source"),
        ),
        sa.CheckConstraint(
            "(operation_mode = 'SINGLE' AND operation_wait_timeout_seconds IS NULL) OR (operation_mode = 'MULTI' AND operation_wait_timeout_seconds >= 30)",
            name=op.f("ck_executions_valid_operation_wait_timeout"),
        ),
        sa.CheckConstraint(
            "code_source_type IN ('INLINE', 'PATH')",
            name=op.f("ck_executions_valid_code_source_type"),
        ),
        sa.CheckConstraint(
            "created_by_type IS NULL OR created_by_type IN ('AGENT', 'USER', 'BATCH')",
            name=op.f("ck_executions_valid_created_by_type"),
        ),
        sa.CheckConstraint(
            "failure_type IS NULL OR failure_type IN ('TOOL_ERROR', 'INFRASTRUCTURE_ERROR', 'WORKER_SHUTDOWN', 'RUNTIME_UNAVAILABLE', 'LEASE_EXPIRED', 'INTERNAL_ERROR', 'OPERATION_WAIT_TIMEOUT', 'OPERATION_TIMEOUT', 'STEP_TIMEOUT', 'EXECUTION_TIMEOUT', 'RUNTIME_SESSION_LOST')",
            name=op.f("ck_executions_valid_failure_type"),
        ),
        sa.CheckConstraint(
            "operation_mode IN ('SINGLE', 'MULTI')", name=op.f("ck_executions_valid_operation_mode")
        ),
        sa.CheckConstraint(
            "retry_strategy IN ('NOT_RETRYABLE', 'FROM_FAILED_STEP', 'FROM_START')",
            name=op.f("ck_executions_valid_retry_strategy"),
        ),
        sa.CheckConstraint(
            "runtime_pool IN ('INTERACTIVE', 'BATCH')",
            name=op.f("ck_executions_valid_runtime_pool"),
        ),
        sa.CheckConstraint(
            "runtime_session_cleanup_status IN ('NOT_REQUIRED', 'PENDING', 'SUCCEEDED', 'FAILED')",
            name=op.f("ck_executions_valid_runtime_session_cleanup_status"),
        ),
        sa.CheckConstraint(
            "runtime_type IN ('JUPYTER')", name=op.f("ck_executions_valid_runtime_type")
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'DISPATCHED', 'RUNNING', 'WAITING_FOR_OPERATION', 'FINALIZING', 'CANCEL_REQUESTED', 'CANCELLED', 'SUCCEEDED', 'FAILED')",
            name=op.f("ck_executions_valid_execution_status"),
        ),
        sa.CheckConstraint(
            "trigger_type IN ('INTERACTIVE', 'BATCH')",
            name=op.f("ck_executions_valid_trigger_type"),
        ),
        sa.CheckConstraint(
            "updated_by_type IS NULL OR updated_by_type IN ('AGENT', 'USER', 'BATCH')",
            name=op.f("ck_executions_valid_updated_by_type"),
        ),
        sa.CheckConstraint(
            "(created_by_type IS NULL) = (created_by IS NULL)",
            name=op.f("ck_executions_complete_created_by"),
        ),
        sa.CheckConstraint(
            "(updated_by_type IS NULL) = (updated_by IS NULL)",
            name=op.f("ck_executions_complete_updated_by"),
        ),
        sa.CheckConstraint(
            "recovery_count >= 0", name=op.f("ck_executions_non_negative_recovery_count")
        ),
        sa.CheckConstraint("retry_count >= 0", name=op.f("ck_executions_non_negative_retry_count")),
        sa.CheckConstraint(
            "retry_from_sequence IS NULL OR retry_from_sequence >= 0",
            name=op.f("ck_executions_non_negative_retry_from_sequence"),
        ),
        sa.ForeignKeyConstraint(
            ["runtime_target_id"],
            ["runtime_targets.id"],
            name=op.f("fk_executions_runtime_target_id_runtime_targets"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_executions")),
        sa.UniqueConstraint(
            "cancel_idempotency_key", name=op.f("uq_executions_cancel_idempotency_key")
        ),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_executions_idempotency_key")),
    )
    op.create_index(
        op.f("ix_executions_active_operation_id"),
        "executions",
        ["active_operation_id"],
        unique=False,
    )
    op.create_index(
        "ix_executions_created_cursor", "executions", ["created_at", "id"], unique=False
    )
    op.create_index(
        op.f("ix_executions_execution_expires_at"),
        "executions",
        ["execution_expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_executions_lease", "executions", ["status", "lease_expires_at"], unique=False
    )
    op.create_index(
        op.f("ix_executions_operation_wait_expires_at"),
        "executions",
        ["operation_wait_expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_executions_project_created_cursor",
        "executions",
        ["project_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_executions_retained_session_cleanup",
        "executions",
        ["status", "retry_strategy", "retained_runtime_session_until"],
        unique=False,
    )
    op.create_index(
        op.f("ix_executions_runtime_target_id"), "executions", ["runtime_target_id"], unique=False
    )
    op.create_index(
        "ix_executions_session_created_cursor",
        "executions",
        ["session_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_executions_status_created_at",
        "executions",
        ["status", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_executions_task_created_cursor",
        "executions",
        ["task_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(op.f("ix_executions_task_id"), "executions", ["task_id"], unique=False)
    op.create_index(
        "ix_executions_user_created_cursor",
        "executions",
        ["user_id", "created_at", "id"],
        unique=False,
    )
    op.create_table(
        "execution_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column(
            "runtime_type",
            sa.Enum("JUPYTER", name="attempt_runtime_type", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("runtime_profile", sa.String(length=128), nullable=False),
        sa.Column("runtime_target_id", sa.Uuid(), nullable=False),
        sa.Column("runtime_session_id", sa.String(length=255), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "RUNNING",
                "WAITING",
                "SUCCEEDED",
                "FAILED",
                "CANCELLED",
                name="attempt_status",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("lease_owner", sa.String(length=255), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "failure_type",
            sa.Enum(
                "TOOL_ERROR",
                "INFRASTRUCTURE_ERROR",
                "WORKER_SHUTDOWN",
                "RUNTIME_UNAVAILABLE",
                "LEASE_EXPIRED",
                "INTERNAL_ERROR",
                "OPERATION_WAIT_TIMEOUT",
                "OPERATION_TIMEOUT",
                "STEP_TIMEOUT",
                "EXECUTION_TIMEOUT",
                "RUNTIME_SESSION_LOST",
                name="attempt_failure_type",
                native_enum=False,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column(
            "retry_strategy",
            sa.Enum(
                "NOT_RETRYABLE",
                "FROM_FAILED_STEP",
                "FROM_START",
                name="attempt_retry_strategy",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "runtime_session_cleanup_status",
            sa.Enum(
                "NOT_REQUIRED",
                "PENDING",
                "SUCCEEDED",
                "FAILED",
                name="attempt_runtime_session_cleanup_status",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "created_by_type",
            sa.Enum("AGENT", "USER", "BATCH", name="actor_type", native_enum=False, length=32),
            nullable=True,
        ),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column(
            "updated_by_type",
            sa.Enum("AGENT", "USER", "BATCH", name="actor_type", native_enum=False, length=32),
            nullable=True,
        ),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "created_by_type IS NULL OR created_by_type IN ('AGENT', 'USER', 'BATCH')",
            name=op.f("ck_execution_attempts_valid_created_by_type"),
        ),
        sa.CheckConstraint(
            "failure_type IS NULL OR failure_type IN ('TOOL_ERROR', 'INFRASTRUCTURE_ERROR', 'WORKER_SHUTDOWN', 'RUNTIME_UNAVAILABLE', 'LEASE_EXPIRED', 'INTERNAL_ERROR', 'OPERATION_WAIT_TIMEOUT', 'OPERATION_TIMEOUT', 'STEP_TIMEOUT', 'EXECUTION_TIMEOUT', 'RUNTIME_SESSION_LOST')",
            name=op.f("ck_execution_attempts_valid_attempt_failure_type"),
        ),
        sa.CheckConstraint(
            "retry_strategy IN ('NOT_RETRYABLE', 'FROM_FAILED_STEP', 'FROM_START')",
            name=op.f("ck_execution_attempts_valid_attempt_retry_strategy"),
        ),
        sa.CheckConstraint(
            "runtime_session_cleanup_status IN ('NOT_REQUIRED', 'PENDING', 'SUCCEEDED', 'FAILED')",
            name=op.f("ck_execution_attempts_valid_runtime_session_cleanup_status"),
        ),
        sa.CheckConstraint(
            "runtime_type IN ('JUPYTER')",
            name=op.f("ck_execution_attempts_valid_attempt_runtime_type"),
        ),
        sa.CheckConstraint(
            "status IN ('RUNNING', 'WAITING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name=op.f("ck_execution_attempts_valid_attempt_status"),
        ),
        sa.CheckConstraint(
            "updated_by_type IS NULL OR updated_by_type IN ('AGENT', 'USER', 'BATCH')",
            name=op.f("ck_execution_attempts_valid_updated_by_type"),
        ),
        sa.CheckConstraint(
            "(created_by_type IS NULL) = (created_by IS NULL)",
            name=op.f("ck_execution_attempts_complete_created_by"),
        ),
        sa.CheckConstraint(
            "(updated_by_type IS NULL) = (updated_by IS NULL)",
            name=op.f("ck_execution_attempts_complete_updated_by"),
        ),
        sa.CheckConstraint(
            "attempt_number > 0", name=op.f("ck_execution_attempts_positive_attempt_number")
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["executions.id"],
            name=op.f("fk_execution_attempts_execution_id_executions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["runtime_target_id"],
            ["runtime_targets.id"],
            name=op.f("fk_execution_attempts_runtime_target_id_runtime_targets"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_execution_attempts")),
        sa.UniqueConstraint(
            "execution_id", "attempt_number", name="uq_execution_attempts_execution_attempt"
        ),
    )
    op.create_index(
        "ix_execution_attempts_lease",
        "execution_attempts",
        ["status", "lease_expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_execution_attempts_target_status",
        "execution_attempts",
        ["runtime_target_id", "status"],
        unique=False,
    )
    op.create_table(
        "execution_operations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("operation_number", sa.Integer(), nullable=False),
        sa.Column("first_sequence", sa.Integer(), nullable=False),
        sa.Column("last_sequence", sa.Integer(), nullable=False),
        sa.Column("operation_timeout_seconds", sa.Integer(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column(
            "code_source_type",
            sa.Enum(
                "INLINE", "PATH", name="operation_code_source_type", native_enum=False, length=32
            ),
            nullable=False,
        ),
        sa.Column("source_content", sa.Text(), nullable=False),
        sa.Column("code_path", sa.Text(), nullable=True),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "QUEUED",
                "RUNNING",
                "SUCCEEDED",
                "FAILED",
                "CANCELLED",
                name="operation_status",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("execution_attempt_id", sa.Uuid(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_by_type",
            sa.Enum("AGENT", "USER", "BATCH", name="actor_type", native_enum=False, length=32),
            nullable=True,
        ),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column(
            "updated_by_type",
            sa.Enum("AGENT", "USER", "BATCH", name="actor_type", native_enum=False, length=32),
            nullable=True,
        ),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "created_by_type IS NULL OR created_by_type IN ('AGENT', 'USER', 'BATCH')",
            name=op.f("ck_execution_operations_valid_created_by_type"),
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name=op.f("ck_execution_operations_valid_operation_status"),
        ),
        sa.CheckConstraint(
            "updated_by_type IS NULL OR updated_by_type IN ('AGENT', 'USER', 'BATCH')",
            name=op.f("ck_execution_operations_valid_updated_by_type"),
        ),
        sa.CheckConstraint(
            "(created_by_type IS NULL) = (created_by IS NULL)",
            name=op.f("ck_execution_operations_complete_created_by"),
        ),
        sa.CheckConstraint(
            "(updated_by_type IS NULL) = (updated_by IS NULL)",
            name=op.f("ck_execution_operations_complete_updated_by"),
        ),
        sa.CheckConstraint(
            "first_sequence >= 0", name=op.f("ck_execution_operations_non_negative_first_sequence")
        ),
        sa.CheckConstraint(
            "last_sequence >= first_sequence",
            name=op.f("ck_execution_operations_valid_operation_sequence_range"),
        ),
        sa.CheckConstraint(
            "operation_number > 0", name=op.f("ck_execution_operations_positive_operation_number")
        ),
        sa.CheckConstraint(
            "operation_timeout_seconds IS NULL OR operation_timeout_seconds >= 1",
            name=op.f("ck_execution_operations_valid_operation_timeout"),
        ),
        sa.ForeignKeyConstraint(
            ["execution_attempt_id"],
            ["execution_attempts.id"],
            name=op.f("fk_execution_operations_execution_attempt_id_execution_attempts"),
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["executions.id"],
            name=op.f("fk_execution_operations_execution_id_executions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_execution_operations")),
        sa.UniqueConstraint(
            "execution_id", "operation_number", name="uq_operations_execution_number"
        ),
        sa.UniqueConstraint(
            "idempotency_key", name=op.f("uq_execution_operations_idempotency_key")
        ),
    )
    op.create_index(
        "ix_execution_operations_execution_number",
        "execution_operations",
        ["execution_id", "operation_number"],
        unique=False,
    )
    op.create_table(
        "execution_retries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("from_sequence", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "from_sequence >= 0", name=op.f("ck_execution_retries_non_negative_from_sequence")
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["executions.id"],
            name=op.f("fk_execution_retries_execution_id_executions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_execution_retries")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_execution_retries_idempotency_key")),
    )
    op.create_index(
        op.f("ix_execution_retries_execution_id"),
        "execution_retries",
        ["execution_id"],
        unique=False,
    )
    op.create_table(
        "execution_steps",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=True),
        sa.Column("step_timeout_seconds", sa.Integer(), nullable=True),
        sa.Column("skill_name", sa.String(length=255), nullable=True),
        sa.Column("tool_name", sa.String(length=255), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "RUNNING",
                "SUCCEEDED",
                "FAILED",
                "SKIPPED",
                "CANCELLED",
                name="step_status",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("input_parameters", sa.JSON(), nullable=False),
        sa.Column("outputs", sa.JSON(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_by_type",
            sa.Enum("AGENT", "USER", "BATCH", name="actor_type", native_enum=False, length=32),
            nullable=True,
        ),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column(
            "updated_by_type",
            sa.Enum("AGENT", "USER", "BATCH", name="actor_type", native_enum=False, length=32),
            nullable=True,
        ),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "created_by_type IS NULL OR created_by_type IN ('AGENT', 'USER', 'BATCH')",
            name=op.f("ck_execution_steps_valid_created_by_type"),
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'SKIPPED', 'CANCELLED')",
            name=op.f("ck_execution_steps_valid_step_status"),
        ),
        sa.CheckConstraint(
            "updated_by_type IS NULL OR updated_by_type IN ('AGENT', 'USER', 'BATCH')",
            name=op.f("ck_execution_steps_valid_updated_by_type"),
        ),
        sa.CheckConstraint(
            "(created_by_type IS NULL) = (created_by IS NULL)",
            name=op.f("ck_execution_steps_complete_created_by"),
        ),
        sa.CheckConstraint(
            "(updated_by_type IS NULL) = (updated_by IS NULL)",
            name=op.f("ck_execution_steps_complete_updated_by"),
        ),
        sa.CheckConstraint("sequence >= 0", name=op.f("ck_execution_steps_non_negative_sequence")),
        sa.CheckConstraint(
            "step_timeout_seconds IS NULL OR step_timeout_seconds >= 1",
            name=op.f("ck_execution_steps_valid_step_timeout"),
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["executions.id"],
            name=op.f("fk_execution_steps_execution_id_executions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["execution_operations.id"],
            name=op.f("fk_execution_steps_operation_id_execution_operations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_execution_steps")),
        sa.UniqueConstraint(
            "execution_id", "sequence", name="uq_execution_steps_execution_sequence"
        ),
    )
    op.create_index(
        op.f("ix_execution_steps_execution_id"), "execution_steps", ["execution_id"], unique=False
    )
    op.create_index(
        op.f("ix_execution_steps_operation_id"), "execution_steps", ["operation_id"], unique=False
    )
    op.create_table(
        "execution_step_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("execution_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("execution_step_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("skill_name", sa.String(length=255), nullable=True),
        sa.Column("tool_name", sa.String(length=255), nullable=True),
        sa.Column("input_parameters", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "RUNNING",
                "SUCCEEDED",
                "FAILED",
                "SKIPPED",
                "CANCELLED",
                name="step_attempt_status",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("outputs", sa.JSON(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_by_type",
            sa.Enum("AGENT", "USER", "BATCH", name="actor_type", native_enum=False, length=32),
            nullable=True,
        ),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column(
            "updated_by_type",
            sa.Enum("AGENT", "USER", "BATCH", name="actor_type", native_enum=False, length=32),
            nullable=True,
        ),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "created_by_type IS NULL OR created_by_type IN ('AGENT', 'USER', 'BATCH')",
            name=op.f("ck_execution_step_attempts_valid_created_by_type"),
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'SKIPPED', 'CANCELLED')",
            name=op.f("ck_execution_step_attempts_valid_step_attempt_status"),
        ),
        sa.CheckConstraint(
            "updated_by_type IS NULL OR updated_by_type IN ('AGENT', 'USER', 'BATCH')",
            name=op.f("ck_execution_step_attempts_valid_updated_by_type"),
        ),
        sa.CheckConstraint(
            "(created_by_type IS NULL) = (created_by IS NULL)",
            name=op.f("ck_execution_step_attempts_complete_created_by"),
        ),
        sa.CheckConstraint(
            "(updated_by_type IS NULL) = (updated_by IS NULL)",
            name=op.f("ck_execution_step_attempts_complete_updated_by"),
        ),
        sa.CheckConstraint(
            "sequence >= 0", name=op.f("ck_execution_step_attempts_non_negative_sequence")
        ),
        sa.ForeignKeyConstraint(
            ["execution_attempt_id"],
            ["execution_attempts.id"],
            name=op.f("fk_execution_step_attempts_execution_attempt_id_execution_attempts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["executions.id"],
            name=op.f("fk_execution_step_attempts_execution_id_executions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["execution_step_id"],
            ["execution_steps.id"],
            name=op.f("fk_execution_step_attempts_execution_step_id_execution_steps"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_execution_step_attempts")),
        sa.UniqueConstraint(
            "execution_attempt_id", "sequence", name="uq_execution_step_attempts_attempt_sequence"
        ),
    )
    op.create_index(
        op.f("ix_execution_step_attempts_execution_attempt_id"),
        "execution_step_attempts",
        ["execution_attempt_id"],
        unique=False,
    )
    op.create_index(
        "ix_step_attempts_execution_sequence",
        "execution_step_attempts",
        ["execution_id", "sequence"],
        unique=False,
    )
    op.create_table(
        "execution_artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("execution_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("execution_step_id", sa.Uuid(), nullable=True),
        sa.Column("execution_step_attempt_id", sa.Uuid(), nullable=True),
        sa.Column("parent_artifact_id", sa.Uuid(), nullable=True),
        sa.Column("external_parent_asset_id", sa.String(length=255), nullable=True),
        sa.Column(
            "artifact_type",
            sa.Enum(
                "DATASET",
                "NOTEBOOK",
                "REPORT",
                "PLOT",
                "MODEL",
                "METRIC",
                "LOG",
                "OTHER",
                name="artifact_type",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "storage_type",
            sa.Enum("PV", "S3", name="artifact_storage_type", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "AVAILABLE",
                "INCOMPLETE",
                "DELETED",
                name="artifact_status",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=True),
        sa.Column("media_type", sa.String(length=255), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("identity_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_by_type",
            sa.Enum("AGENT", "USER", "BATCH", name="actor_type", native_enum=False, length=32),
            nullable=True,
        ),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column(
            "updated_by_type",
            sa.Enum("AGENT", "USER", "BATCH", name="actor_type", native_enum=False, length=32),
            nullable=True,
        ),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "artifact_type IN ('DATASET', 'NOTEBOOK', 'REPORT', 'PLOT', 'MODEL', 'METRIC', 'LOG', 'OTHER')",
            name=op.f("ck_execution_artifacts_valid_artifact_type"),
        ),
        sa.CheckConstraint(
            "created_by_type IS NULL OR created_by_type IN ('AGENT', 'USER', 'BATCH')",
            name=op.f("ck_execution_artifacts_valid_created_by_type"),
        ),
        sa.CheckConstraint(
            "status IN ('AVAILABLE', 'INCOMPLETE', 'DELETED')",
            name=op.f("ck_execution_artifacts_valid_artifact_status"),
        ),
        sa.CheckConstraint(
            "storage_type IN ('PV', 'S3')",
            name=op.f("ck_execution_artifacts_valid_artifact_storage_type"),
        ),
        sa.CheckConstraint(
            "updated_by_type IS NULL OR updated_by_type IN ('AGENT', 'USER', 'BATCH')",
            name=op.f("ck_execution_artifacts_valid_updated_by_type"),
        ),
        sa.CheckConstraint(
            "(created_by_type IS NULL) = (created_by IS NULL)",
            name=op.f("ck_execution_artifacts_complete_created_by"),
        ),
        sa.CheckConstraint(
            "(updated_by_type IS NULL) = (updated_by IS NULL)",
            name=op.f("ck_execution_artifacts_complete_updated_by"),
        ),
        sa.CheckConstraint(
            "size_bytes IS NULL OR size_bytes >= 0",
            name=op.f("ck_execution_artifacts_non_negative_size"),
        ),
        sa.ForeignKeyConstraint(
            ["execution_attempt_id"],
            ["execution_attempts.id"],
            name=op.f("fk_execution_artifacts_execution_attempt_id_execution_attempts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["executions.id"],
            name=op.f("fk_execution_artifacts_execution_id_executions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["execution_step_attempt_id"],
            ["execution_step_attempts.id"],
            name=op.f("fk_execution_artifacts_execution_step_attempt_id_execution_step_attempts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["execution_step_id"],
            ["execution_steps.id"],
            name=op.f("fk_execution_artifacts_execution_step_id_execution_steps"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["parent_artifact_id"],
            ["execution_artifacts.id"],
            name=op.f("fk_execution_artifacts_parent_artifact_id_execution_artifacts"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_execution_artifacts")),
        sa.UniqueConstraint("identity_hash", name=op.f("uq_execution_artifacts_identity_hash")),
    )
    op.create_index(
        "ix_execution_artifacts_execution_created",
        "execution_artifacts",
        ["execution_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_execution_artifacts_step",
        "execution_artifacts",
        ["execution_step_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_execution_artifacts_step", table_name="execution_artifacts")
    op.drop_index("ix_execution_artifacts_execution_created", table_name="execution_artifacts")
    op.drop_table("execution_artifacts")
    op.drop_index("ix_step_attempts_execution_sequence", table_name="execution_step_attempts")
    op.drop_index(
        op.f("ix_execution_step_attempts_execution_attempt_id"),
        table_name="execution_step_attempts",
    )
    op.drop_table("execution_step_attempts")
    op.drop_index(op.f("ix_execution_steps_operation_id"), table_name="execution_steps")
    op.drop_index(op.f("ix_execution_steps_execution_id"), table_name="execution_steps")
    op.drop_table("execution_steps")
    op.drop_index(op.f("ix_execution_retries_execution_id"), table_name="execution_retries")
    op.drop_table("execution_retries")
    op.drop_index("ix_execution_operations_execution_number", table_name="execution_operations")
    op.drop_table("execution_operations")
    op.drop_index("ix_execution_attempts_target_status", table_name="execution_attempts")
    op.drop_index("ix_execution_attempts_lease", table_name="execution_attempts")
    op.drop_table("execution_attempts")
    op.drop_index("ix_executions_user_created_cursor", table_name="executions")
    op.drop_index(op.f("ix_executions_task_id"), table_name="executions")
    op.drop_index("ix_executions_task_created_cursor", table_name="executions")
    op.drop_index("ix_executions_status_created_at", table_name="executions")
    op.drop_index("ix_executions_session_created_cursor", table_name="executions")
    op.drop_index(op.f("ix_executions_runtime_target_id"), table_name="executions")
    op.drop_index("ix_executions_retained_session_cleanup", table_name="executions")
    op.drop_index("ix_executions_project_created_cursor", table_name="executions")
    op.drop_index(op.f("ix_executions_operation_wait_expires_at"), table_name="executions")
    op.drop_index("ix_executions_lease", table_name="executions")
    op.drop_index(op.f("ix_executions_execution_expires_at"), table_name="executions")
    op.drop_index("ix_executions_created_cursor", table_name="executions")
    op.drop_index(op.f("ix_executions_active_operation_id"), table_name="executions")
    op.drop_table("executions")
    op.drop_index("ix_runtime_targets_pool_status", table_name="runtime_targets")
    op.drop_index("ix_runtime_targets_created_cursor", table_name="runtime_targets")
    op.drop_table("runtime_targets")
    op.drop_table("runtime_target_purges")
    op.drop_index("ix_outbox_pending", table_name="outbox_events")
    op.drop_index("ix_outbox_execution_cursor", table_name="outbox_events")
    op.drop_index(op.f("ix_outbox_events_aggregate_id"), table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_table("command_receipts")
