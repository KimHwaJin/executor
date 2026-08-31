"""Execution retry and attempt history SQLAlchemy ORM models."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from executor_service.domain.enums import (
    ActorType,
    AttemptStatus,
    FailureType,
    RetryStrategy,
    RuntimeAbortStatus,
    RuntimeSessionCleanupStatus,
    RuntimeType,
    StepStatus,
)
from executor_service.domain.models import empty_output_summary, utc_now
from executor_service.infrastructure.db._models.common import (
    audit_actor_constraints,
    enum_type,
)
from executor_service.infrastructure.db.base import Base


class ExecutionRetryORM(Base):
    __tablename__ = "execution_retries"
    __table_args__ = (
        CheckConstraint(
            "from_sequence >= 0", name="non_negative_from_sequence"
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    execution_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("executions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True
    )
    from_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class ExecutionAttemptORM(Base):
    __tablename__ = "execution_attempts"
    __table_args__ = (
        *audit_actor_constraints(),
        UniqueConstraint(
            "execution_id",
            "attempt_number",
            name="uq_execution_attempts_execution_attempt",
        ),
        CheckConstraint("attempt_number > 0", name="positive_attempt_number"),
        CheckConstraint(
            "status IN ('RUNNING', 'WAITING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name="valid_attempt_status",
        ),
        CheckConstraint(
            "runtime_type IN ('JUPYTER')", name="valid_attempt_runtime_type"
        ),
        CheckConstraint(
            "failure_type IS NULL OR failure_type IN ('TOOL_ERROR', "
            "'INFRASTRUCTURE_ERROR', 'WORKER_SHUTDOWN', 'RUNTIME_UNAVAILABLE', "
            "'LEASE_EXPIRED', 'INTERNAL_ERROR', 'OPERATION_WAIT_TIMEOUT', "
            "'OPERATION_TIMEOUT', 'STEP_TIMEOUT', 'EXECUTION_TIMEOUT', "
            "'OUTPUT_LIMIT_EXCEEDED', "
            "'RUNTIME_SESSION_LOST', 'COMPLETION_FAILED')",
            name="valid_attempt_failure_type",
        ),
        CheckConstraint(
            "retry_strategy IN ('NOT_RETRYABLE', 'FROM_FAILED_STEP', 'FROM_START')",
            name="valid_attempt_retry_strategy",
        ),
        CheckConstraint(
            "runtime_session_cleanup_status IN ('NOT_REQUIRED', 'PENDING', 'SUCCEEDED', 'FAILED')",
            name="valid_runtime_session_cleanup_status",
        ),
        CheckConstraint(
            "runtime_abort_status IN ('NOT_REQUIRED', 'PENDING', "
            "'IDLE_CONFIRMED', 'SESSION_DELETED', 'SESSION_MISSING', 'FAILED')",
            name="valid_runtime_abort_status",
        ),
        CheckConstraint(
            "fencing_token >= 0", name="non_negative_fencing_token"
        ),
        Index("ix_execution_attempts_lease", "status", "lease_expires_at"),
        Index(
            "ix_execution_attempts_target_status",
            "runtime_target_id",
            "status",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    execution_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("executions.id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    runtime_type: Mapped[RuntimeType] = mapped_column(
        enum_type(RuntimeType, "attempt_runtime_type"),
        nullable=False,
        default=RuntimeType.JUPYTER,
    )
    runtime_profile: Mapped[str] = mapped_column(
        String(128), nullable=False, default="basic"
    )
    runtime_target_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("runtime_targets.id"), nullable=False
    )
    runtime_session_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[AttemptStatus] = mapped_column(
        enum_type(AttemptStatus, "attempt_status"), nullable=False
    )
    lease_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    fencing_token: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    failure_type: Mapped[FailureType | None] = mapped_column(
        enum_type(FailureType, "attempt_failure_type")
    )
    retry_strategy: Mapped[RetryStrategy] = mapped_column(
        enum_type(RetryStrategy, "attempt_retry_strategy"),
        nullable=False,
        default=RetryStrategy.NOT_RETRYABLE,
    )
    runtime_session_cleanup_status: Mapped[RuntimeSessionCleanupStatus] = (
        mapped_column(
            enum_type(
                RuntimeSessionCleanupStatus,
                "attempt_runtime_session_cleanup_status",
            ),
            nullable=False,
            default=RuntimeSessionCleanupStatus.NOT_REQUIRED,
        )
    )
    runtime_abort_status: Mapped[RuntimeAbortStatus] = mapped_column(
        enum_type(RuntimeAbortStatus, "attempt_runtime_abort_status"),
        nullable=False,
        default=RuntimeAbortStatus.NOT_REQUIRED,
    )
    created_by_type: Mapped[ActorType | None] = mapped_column(
        enum_type(ActorType, "actor_type"), nullable=True
    )
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_by_type: Mapped[ActorType | None] = mapped_column(
        enum_type(ActorType, "actor_type"), nullable=True
    )
    updated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


class ExecutionStepAttemptORM(Base):
    """Immutable-per-attempt Step result history used for end-to-end tracing."""

    __tablename__ = "execution_step_attempts"
    __table_args__ = (
        *audit_actor_constraints(),
        UniqueConstraint(
            "execution_attempt_id",
            "sequence",
            name="uq_execution_step_attempts_attempt_sequence",
        ),
        CheckConstraint("sequence >= 0", name="non_negative_sequence"),
        CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'SKIPPED', 'CANCELLED')",
            name="valid_step_attempt_status",
        ),
        Index(
            "ix_step_attempts_execution_sequence", "execution_id", "sequence"
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    execution_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("executions.id", ondelete="CASCADE"),
        nullable=False,
    )
    execution_attempt_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("execution_attempts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    execution_step_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("execution_steps.id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    skill_name: Mapped[str | None] = mapped_column(String(255))
    tool_name: Mapped[str | None] = mapped_column(String(255))
    input_parameters: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    status: Mapped[StepStatus] = mapped_column(
        enum_type(StepStatus, "step_attempt_status"), nullable=False
    )
    output_summary: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=empty_output_summary
    )
    result_manifest_path: Mapped[str | None] = mapped_column(Text)
    result_manifest_checksum_sha256: Mapped[str | None] = mapped_column(
        String(64)
    )
    result_manifest_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    result_fencing_token: Mapped[int | None] = mapped_column(BigInteger)
    result_complete: Mapped[bool | None] = mapped_column(Boolean)
    result_representation_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    result_total_size_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    created_by_type: Mapped[ActorType | None] = mapped_column(
        enum_type(ActorType, "actor_type"), nullable=True
    )
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_by_type: Mapped[ActorType | None] = mapped_column(
        enum_type(ActorType, "actor_type"), nullable=True
    )
    updated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
