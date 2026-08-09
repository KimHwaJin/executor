"""Persistence-only SQLAlchemy models."""

from datetime import datetime
from enum import Enum as PythonEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from executor_service.domain.enums import (
    ArtifactStatus,
    ArtifactStorageType,
    ArtifactType,
    AttemptStatus,
    CodeSourceType,
    ExecutionMode,
    ExecutionStatus,
    FailureType,
    JupyterPool,
    JupyterServerStatus,
    KernelCleanupStatus,
    OutboxStatus,
    RetryStrategy,
    StepStatus,
    TriggerType,
)
from executor_service.domain.models import Execution, ExecutionStep, OutboxEvent, utc_now
from executor_service.infrastructure.db.base import Base


def enum_type(enum_class: type[PythonEnum], name: str) -> Enum:
    return Enum(enum_class, name=name, native_enum=False, create_constraint=False, length=32)


class ExecutionORM(Base):
    __tablename__ = "executions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED', 'DISPATCHED', 'RUNNING', 'CANCEL_REQUESTED', "
            "'CANCELLED', 'SUCCEEDED', 'FAILED')",
            name="valid_execution_status",
        ),
        CheckConstraint("mode IN ('STATIC', 'DYNAMIC')", name="valid_execution_mode"),
        CheckConstraint("trigger_type IN ('INTERACTIVE', 'BATCH')", name="valid_trigger_type"),
        CheckConstraint("jupyter_pool IN ('INTERACTIVE', 'BATCH')", name="valid_jupyter_pool"),
        CheckConstraint("code_source_type IN ('INLINE', 'PATH')", name="valid_code_source_type"),
        CheckConstraint(
            "(code_source_type = 'INLINE' AND code IS NOT NULL AND code_path IS NULL) OR "
            "(code_source_type = 'PATH' AND code IS NULL AND code_path IS NOT NULL)",
            name="valid_code_source",
        ),
        CheckConstraint("retry_count >= 0", name="non_negative_retry_count"),
        CheckConstraint("recovery_count >= 0", name="non_negative_recovery_count"),
        CheckConstraint(
            "failure_type IS NULL OR failure_type IN ('TOOL_ERROR', "
            "'INFRASTRUCTURE_ERROR', 'WORKER_SHUTDOWN', 'JUPYTER_UNAVAILABLE', "
            "'LEASE_EXPIRED', 'INTERNAL_ERROR')",
            name="valid_failure_type",
        ),
        CheckConstraint(
            "retry_strategy IN ('NOT_RETRYABLE', 'FROM_FAILED_STEP', 'FROM_START')",
            name="valid_retry_strategy",
        ),
        CheckConstraint(
            "kernel_cleanup_status IN ('NOT_REQUIRED', 'PENDING', 'SUCCEEDED', 'FAILED')",
            name="valid_kernel_cleanup_status",
        ),
        CheckConstraint(
            "retry_from_sequence IS NULL OR retry_from_sequence >= 0",
            name="non_negative_retry_from_sequence",
        ),
        Index("ix_executions_status_created_at", "status", "created_at"),
        Index("ix_executions_lease", "status", "lease_expires_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    cancel_idempotency_key: Mapped[str | None] = mapped_column(
        String(255), unique=True, nullable=True
    )

    status: Mapped[ExecutionStatus] = mapped_column(
        enum_type(ExecutionStatus, "execution_status"), nullable=False
    )
    mode: Mapped[ExecutionMode] = mapped_column(
        enum_type(ExecutionMode, "execution_mode"), nullable=False
    )
    trigger_type: Mapped[TriggerType] = mapped_column(
        enum_type(TriggerType, "trigger_type"), nullable=False
    )
    jupyter_pool: Mapped[JupyterPool] = mapped_column(
        enum_type(JupyterPool, "jupyter_pool"), nullable=False
    )
    kernel_name: Mapped[str] = mapped_column(String(128), nullable=False)
    code_source_type: Mapped[CodeSourceType] = mapped_column(
        enum_type(CodeSourceType, "code_source_type"), nullable=False
    )
    code: Mapped[str | None] = mapped_column(Text, nullable=True)
    code_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    requested_by_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    project_id: Mapped[str] = mapped_column(String(255), nullable=False)
    session_id: Mapped[str] = mapped_column(String(255), nullable=False)
    execution_plan_id: Mapped[str] = mapped_column(String(255), nullable=False)
    workflow_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    execution_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict
    )
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    jupyter_server_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jupyter_servers.id"), nullable=True, index=True
    )
    kernel_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    workspace_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    notebook_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_type: Mapped[FailureType | None] = mapped_column(
        enum_type(FailureType, "failure_type"), nullable=True
    )
    lease_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    retry_strategy: Mapped[RetryStrategy] = mapped_column(
        enum_type(RetryStrategy, "retry_strategy"),
        nullable=False,
        default=RetryStrategy.NOT_RETRYABLE,
    )
    retry_from_sequence: Mapped[int | None] = mapped_column(Integer)
    retained_kernel_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recovery_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    kernel_cleanup_status: Mapped[KernelCleanupStatus] = mapped_column(
        enum_type(KernelCleanupStatus, "kernel_cleanup_status"),
        nullable=False,
        default=KernelCleanupStatus.NOT_REQUIRED,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    steps: Mapped[list["ExecutionStepORM"]] = relationship(
        back_populates="execution",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="ExecutionStepORM.sequence",
    )

    @classmethod
    def from_domain(cls, execution: Execution) -> "ExecutionORM":
        return cls(
            id=execution.id,
            idempotency_key=execution.idempotency_key,
            request_fingerprint=execution.request_fingerprint,
            cancel_idempotency_key=execution.cancel_idempotency_key,
            status=execution.status,
            mode=execution.mode,
            trigger_type=execution.trigger_type,
            jupyter_pool=execution.jupyter_pool,
            kernel_name=execution.kernel_name,
            code_source_type=execution.code_source_type,
            code=execution.code,
            code_path=execution.code_path,
            requested_by_user_id=execution.requested_by_user_id,
            project_id=execution.project_id,
            session_id=execution.session_id,
            execution_plan_id=execution.execution_plan_id,
            workflow_id=execution.workflow_id,
            correlation_id=execution.correlation_id,
            execution_metadata=execution.metadata,
            cancellation_reason=execution.cancellation_reason,
            jupyter_server_id=execution.jupyter_server_id,
            kernel_id=execution.kernel_id,
            workspace_path=execution.workspace_path,
            notebook_path=execution.notebook_path,
            error_message=execution.error_message,
            failure_type=execution.failure_type,
            lease_owner=execution.lease_owner,
            lease_expires_at=execution.lease_expires_at,
            heartbeat_at=execution.heartbeat_at,
            retryable=execution.retryable,
            retry_strategy=execution.retry_strategy,
            retry_from_sequence=execution.retry_from_sequence,
            retained_kernel_until=execution.retained_kernel_until,
            retry_count=execution.retry_count,
            recovery_count=execution.recovery_count,
            kernel_cleanup_status=execution.kernel_cleanup_status,
            version=execution.version,
            created_at=execution.created_at,
            updated_at=execution.updated_at,
            started_at=execution.started_at,
            finished_at=execution.finished_at,
            steps=[ExecutionStepORM.from_domain(step) for step in execution.steps],
        )

    def to_domain(self) -> Execution:
        return Execution(
            id=self.id,
            idempotency_key=self.idempotency_key,
            request_fingerprint=self.request_fingerprint,
            cancel_idempotency_key=self.cancel_idempotency_key,
            status=self.status,
            mode=self.mode,
            trigger_type=self.trigger_type,
            jupyter_pool=self.jupyter_pool,
            kernel_name=self.kernel_name,
            code_source_type=self.code_source_type,
            code=self.code,
            code_path=self.code_path,
            requested_by_user_id=self.requested_by_user_id,
            project_id=self.project_id,
            session_id=self.session_id,
            execution_plan_id=self.execution_plan_id,
            workflow_id=self.workflow_id,
            correlation_id=self.correlation_id,
            metadata=self.execution_metadata,
            cancellation_reason=self.cancellation_reason,
            jupyter_server_id=self.jupyter_server_id,
            kernel_id=self.kernel_id,
            workspace_path=self.workspace_path,
            notebook_path=self.notebook_path,
            error_message=self.error_message,
            failure_type=self.failure_type,
            lease_owner=self.lease_owner,
            lease_expires_at=self.lease_expires_at,
            heartbeat_at=self.heartbeat_at,
            retryable=self.retryable,
            retry_strategy=self.retry_strategy,
            retry_from_sequence=self.retry_from_sequence,
            retained_kernel_until=self.retained_kernel_until,
            retry_count=self.retry_count,
            recovery_count=self.recovery_count,
            kernel_cleanup_status=self.kernel_cleanup_status,
            version=self.version,
            created_at=self.created_at,
            updated_at=self.updated_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            steps=[step.to_domain() for step in self.steps],
        )


class ExecutionStepORM(Base):
    __tablename__ = "execution_steps"
    __table_args__ = (
        UniqueConstraint("execution_id", "sequence", name="uq_execution_steps_execution_sequence"),
        CheckConstraint("sequence >= 0", name="non_negative_sequence"),
        CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'SKIPPED', 'CANCELLED')",
            name="valid_step_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    execution_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("executions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    skill_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[StepStatus] = mapped_column(
        enum_type(StepStatus, "step_status"), nullable=False, default=StepStatus.PENDING
    )
    input_parameters: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    outputs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    execution: Mapped[ExecutionORM] = relationship(back_populates="steps")

    @classmethod
    def from_domain(cls, step: ExecutionStep) -> "ExecutionStepORM":
        return cls(
            id=step.id,
            sequence=step.sequence,
            skill_name=step.skill_name,
            tool_name=step.tool_name,
            status=step.status,
            input_parameters=step.input_parameters,
            outputs=step.outputs,
            error_message=step.error_message,
            created_at=step.created_at,
            updated_at=step.updated_at,
            started_at=step.started_at,
            finished_at=step.finished_at,
        )

    def to_domain(self) -> ExecutionStep:
        return ExecutionStep(
            id=self.id,
            sequence=self.sequence,
            skill_name=self.skill_name,
            tool_name=self.tool_name,
            status=self.status,
            input_parameters=self.input_parameters,
            outputs=self.outputs,
            error_message=self.error_message,
            created_at=self.created_at,
            updated_at=self.updated_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
        )


class JupyterServerORM(Base):
    __tablename__ = "jupyter_servers"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE', 'DRAINING', 'OFFLINE')", name="valid_jupyter_server_status"
        ),
        CheckConstraint("max_concurrent_executions > 0", name="positive_max_concurrency"),
        Index("ix_jupyter_servers_pool_status", "pool", "enabled", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    credential_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    credential_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    pool: Mapped[JupyterPool] = mapped_column(
        enum_type(JupyterPool, "jupyter_server_pool"), nullable=False
    )
    status: Mapped[JupyterServerStatus] = mapped_column(
        enum_type(JupyterServerStatus, "jupyter_server_status"), nullable=False
    )
    max_concurrent_executions: Mapped[int] = mapped_column(Integer, nullable=False)
    supported_kernels: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_health_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_health_error: Mapped[str | None] = mapped_column(String(500))
    active_kernel_count: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CommandReceiptORM(Base):
    """Idempotency receipt shared by non-execution mutating commands."""

    __tablename__ = "command_receipts"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    command_type: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class ExecutionRetryORM(Base):
    __tablename__ = "execution_retries"
    __table_args__ = (
        CheckConstraint("from_sequence >= 0", name="non_negative_from_sequence"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    execution_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("executions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    from_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class ExecutionAttemptORM(Base):
    __tablename__ = "execution_attempts"
    __table_args__ = (
        UniqueConstraint(
            "execution_id", "attempt_number", name="uq_execution_attempts_execution_attempt"
        ),
        CheckConstraint("attempt_number > 0", name="positive_attempt_number"),
        CheckConstraint(
            "status IN ('RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name="valid_attempt_status",
        ),
        CheckConstraint(
            "failure_type IS NULL OR failure_type IN ('TOOL_ERROR', "
            "'INFRASTRUCTURE_ERROR', 'WORKER_SHUTDOWN', 'JUPYTER_UNAVAILABLE', "
            "'LEASE_EXPIRED', 'INTERNAL_ERROR')",
            name="valid_attempt_failure_type",
        ),
        CheckConstraint(
            "retry_strategy IN ('NOT_RETRYABLE', 'FROM_FAILED_STEP', 'FROM_START')",
            name="valid_attempt_retry_strategy",
        ),
        CheckConstraint(
            "kernel_cleanup_status IN ('NOT_REQUIRED', 'PENDING', 'SUCCEEDED', 'FAILED')",
            name="valid_attempt_kernel_cleanup_status",
        ),
        Index("ix_execution_attempts_lease", "status", "lease_expires_at"),
        Index("ix_execution_attempts_server_status", "jupyter_server_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    execution_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("executions.id", ondelete="CASCADE"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    jupyter_server_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jupyter_servers.id"), nullable=False
    )
    kernel_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[AttemptStatus] = mapped_column(
        enum_type(AttemptStatus, "attempt_status"), nullable=False
    )
    lease_owner: Mapped[str] = mapped_column(String(255), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    failure_type: Mapped[FailureType | None] = mapped_column(
        enum_type(FailureType, "attempt_failure_type")
    )
    retry_strategy: Mapped[RetryStrategy] = mapped_column(
        enum_type(RetryStrategy, "attempt_retry_strategy"),
        nullable=False,
        default=RetryStrategy.NOT_RETRYABLE,
    )
    kernel_cleanup_status: Mapped[KernelCleanupStatus] = mapped_column(
        enum_type(KernelCleanupStatus, "attempt_kernel_cleanup_status"),
        nullable=False,
        default=KernelCleanupStatus.NOT_REQUIRED,
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExecutionStepAttemptORM(Base):
    """Immutable-per-attempt Step result history used for end-to-end tracing."""

    __tablename__ = "execution_step_attempts"
    __table_args__ = (
        UniqueConstraint(
            "execution_attempt_id",
            "sequence",
            name="uq_execution_step_attempts_attempt_sequence",
        ),
        CheckConstraint("sequence >= 0", name="non_negative_sequence"),
        CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', "
            "'SKIPPED', 'CANCELLED')",
            name="valid_step_attempt_status",
        ),
        Index("ix_step_attempts_execution_sequence", "execution_id", "sequence"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
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
    input_parameters: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[StepStatus] = mapped_column(
        enum_type(StepStatus, "step_attempt_status"), nullable=False
    )
    outputs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExecutionArtifactORM(Base):
    """Artifact evidence produced by one execution Attempt and optionally one Step."""

    __tablename__ = "execution_artifacts"
    __table_args__ = (
        CheckConstraint(
            "artifact_type IN ('DATASET', 'NOTEBOOK', 'REPORT', 'PLOT', 'MODEL', "
            "'METRIC', 'LOG', 'OTHER')",
            name="valid_artifact_type",
        ),
        CheckConstraint(
            "storage_type IN ('PV', 'S3')", name="valid_artifact_storage_type"
        ),
        CheckConstraint(
            "status IN ('AVAILABLE', 'INCOMPLETE', 'DELETED')",
            name="valid_artifact_status",
        ),
        CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="non_negative_size"),
        Index("ix_execution_artifacts_execution_created", "execution_id", "created_at"),
        Index("ix_execution_artifacts_step", "execution_step_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    execution_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("executions.id", ondelete="CASCADE"),
        nullable=False,
    )
    execution_attempt_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("execution_attempts.id", ondelete="CASCADE"),
        nullable=False,
    )
    execution_step_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("execution_steps.id", ondelete="SET NULL"),
    )
    execution_step_attempt_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("execution_step_attempts.id", ondelete="SET NULL"),
    )
    parent_artifact_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("execution_artifacts.id", ondelete="SET NULL")
    )
    external_parent_asset_id: Mapped[str | None] = mapped_column(String(255))
    artifact_type: Mapped[ArtifactType] = mapped_column(
        enum_type(ArtifactType, "artifact_type"), nullable=False
    )
    storage_type: Mapped[ArtifactStorageType] = mapped_column(
        enum_type(ArtifactStorageType, "artifact_storage_type"), nullable=False
    )
    status: Mapped[ArtifactStatus] = mapped_column(
        enum_type(ArtifactStatus, "artifact_status"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    uri: Mapped[str] = mapped_column(Text, nullable=False)
    relative_path: Mapped[str | None] = mapped_column(Text)
    media_type: Mapped[str | None] = mapped_column(String(255))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64))
    artifact_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict
    )
    identity_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class OutboxEventORM(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint("status IN ('PENDING', 'PUBLISHED')", name="valid_outbox_status"),
        Index("ix_outbox_pending", "status", "available_at", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    aggregate_type: Mapped[str] = mapped_column(String(128), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[OutboxStatus] = mapped_column(
        enum_type(OutboxStatus, "outbox_status"), nullable=False
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    @classmethod
    def from_domain(cls, event: OutboxEvent) -> "OutboxEventORM":
        return cls(
            id=event.id,
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            event_type=event.event_type,
            payload=event.payload,
            status=event.status,
            attempt_count=event.attempt_count,
            available_at=event.available_at,
            created_at=event.created_at,
            published_at=event.published_at,
            last_error=event.last_error,
        )
