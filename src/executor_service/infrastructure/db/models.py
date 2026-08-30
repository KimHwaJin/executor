"""Persistence-only SQLAlchemy models."""

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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from executor_service.domain.enums import (
    ActorType,
    ArtifactStatus,
    ArtifactStorageType,
    ArtifactType,
    AttemptStatus,
    CodeSourceType,
    ExecutionStatus,
    FailureType,
    OperationMode,
    OperationStatus,
    OutboxDestination,
    OutboxStatus,
    RetryStrategy,
    RuntimeAbortStatus,
    RuntimePool,
    RuntimeSessionCleanupStatus,
    RuntimeType,
    StepStatus,
    TriggerType,
)
from executor_service.domain.models import (
    Execution,
    ExecutionEvent,
    ExecutionOperation,
    ExecutionStep,
    NotebookProjectionStatus,
    OutboxEvent,
    empty_output_summary,
    utc_now,
)
from executor_service.infrastructure.db import _models as auxiliary_models
from executor_service.infrastructure.db._models import (
    audit_actor_constraints,
    enum_type,
)
from executor_service.infrastructure.db.base import Base

CommandReceiptORM = auxiliary_models.CommandReceiptORM
EventRetentionLeaseORM = auxiliary_models.EventRetentionLeaseORM
ExecutorMaintenanceORM = auxiliary_models.ExecutorMaintenanceORM
MaintenanceRunORM = auxiliary_models.MaintenanceRunORM
MaintenanceRunTargetORM = auxiliary_models.MaintenanceRunTargetORM
RuntimeTargetORM = auxiliary_models.RuntimeTargetORM
RuntimeTargetPurgeORM = auxiliary_models.RuntimeTargetPurgeORM


class ExecutionORM(Base):
    __tablename__ = "executions"
    __table_args__ = (
        *audit_actor_constraints(),
        CheckConstraint(
            "status IN ('QUEUED', 'DISPATCHED', 'RUNNING', 'WAITING_FOR_OPERATION', "
            "'FINALIZING', 'CANCEL_REQUESTED', 'CANCELLED', 'SUCCEEDED', 'FAILED')",
            name="valid_execution_status",
        ),
        CheckConstraint(
            "operation_mode IN ('SINGLE', 'MULTI')",
            name="valid_operation_mode",
        ),
        CheckConstraint(
            "(operation_mode = 'SINGLE' AND operation_wait_timeout_seconds IS NULL) OR "
            "(operation_mode = 'MULTI' AND operation_wait_timeout_seconds >= 30)",
            name="valid_operation_wait_timeout",
        ),
        CheckConstraint(
            "trigger_type IN ('INTERACTIVE', 'BATCH')",
            name="valid_trigger_type",
        ),
        CheckConstraint(
            "runtime_pool IN ('INTERACTIVE', 'BATCH')",
            name="valid_runtime_pool",
        ),
        CheckConstraint(
            "runtime_type IN ('JUPYTER')", name="valid_runtime_type"
        ),
        CheckConstraint("retry_count >= 0", name="non_negative_retry_count"),
        CheckConstraint(
            "recovery_count >= 0", name="non_negative_recovery_count"
        ),
        CheckConstraint(
            "failure_type IS NULL OR failure_type IN ('TOOL_ERROR', "
            "'INFRASTRUCTURE_ERROR', 'WORKER_SHUTDOWN', 'RUNTIME_UNAVAILABLE', "
            "'LEASE_EXPIRED', 'INTERNAL_ERROR', 'OPERATION_WAIT_TIMEOUT', "
            "'OPERATION_TIMEOUT', 'STEP_TIMEOUT', 'EXECUTION_TIMEOUT', "
            "'OUTPUT_LIMIT_EXCEEDED', "
            "'RUNTIME_SESSION_LOST')",
            name="valid_failure_type",
        ),
        CheckConstraint(
            "retry_strategy IN ('NOT_RETRYABLE', 'FROM_FAILED_STEP', 'FROM_START')",
            name="valid_retry_strategy",
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
            "retry_from_sequence IS NULL OR retry_from_sequence >= 0",
            name="non_negative_retry_from_sequence",
        ),
        CheckConstraint(
            "fencing_token >= 0", name="non_negative_fencing_token"
        ),
        CheckConstraint(
            "notebook_projection_status IN "
            "('NOT_STARTED', 'PENDING', 'SUCCEEDED', 'FAILED')",
            name="valid_notebook_projection_status",
        ),
        CheckConstraint(
            "notebook_projection_attempt_count >= 0",
            name="non_negative_notebook_projection_attempt_count",
        ),
        Index("ix_executions_status_created_at", "status", "created_at", "id"),
        Index("ix_executions_created_cursor", "created_at", "id"),
        Index(
            "ix_executions_user_created_cursor",
            "user_id",
            "created_at",
            "id",
        ),
        Index(
            "ix_executions_project_created_cursor",
            "project_id",
            "created_at",
            "id",
        ),
        Index(
            "ix_executions_session_created_cursor",
            "session_id",
            "created_at",
            "id",
        ),
        Index(
            "ix_executions_task_created_cursor", "task_id", "created_at", "id"
        ),
        Index("ix_executions_lease", "status", "lease_expires_at"),
        Index(
            "ix_executions_cancellation_lease",
            "status",
            "cancellation_lease_expires_at",
        ),
        Index(
            "ix_executions_retained_session_cleanup",
            "status",
            "retry_strategy",
            "retained_runtime_session_until",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False
    )
    request_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    cancel_idempotency_key: Mapped[str | None] = mapped_column(
        String(255), unique=True, nullable=True
    )

    status: Mapped[ExecutionStatus] = mapped_column(
        enum_type(ExecutionStatus, "execution_status"), nullable=False
    )
    operation_mode: Mapped[OperationMode] = mapped_column(
        enum_type(OperationMode, "operation_mode"), nullable=False
    )
    operation_wait_timeout_seconds: Mapped[int | None] = mapped_column(Integer)
    trigger_type: Mapped[TriggerType] = mapped_column(
        enum_type(TriggerType, "trigger_type"), nullable=False
    )
    runtime_type: Mapped[RuntimeType] = mapped_column(
        enum_type(RuntimeType, "runtime_type"),
        nullable=False,
        default=RuntimeType.JUPYTER,
    )
    runtime_pool: Mapped[RuntimePool] = mapped_column(
        enum_type(RuntimePool, "runtime_pool"), nullable=False
    )
    runtime_profile: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    task_id: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True
    )
    workflow_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    execution_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict
    )
    cancellation_reason: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    cancellation_lease_owner: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    cancellation_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancellation_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    runtime_target_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("runtime_targets.id"),
        nullable=True,
        index=True,
    )
    runtime_session_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    workspace_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    notebook_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    notebook_projection_status: Mapped[NotebookProjectionStatus] = (
        mapped_column(String(16), nullable=False, default="NOT_STARTED")
    )
    notebook_projection_attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    notebook_projection_error: Mapped[str | None] = mapped_column(Text)
    notebook_projected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_type: Mapped[FailureType | None] = mapped_column(
        enum_type(FailureType, "failure_type"), nullable=True
    )
    lease_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    fencing_token: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    retry_strategy: Mapped[RetryStrategy] = mapped_column(
        enum_type(RetryStrategy, "retry_strategy"),
        nullable=False,
        default=RetryStrategy.NOT_RETRYABLE,
    )
    retry_from_sequence: Mapped[int | None] = mapped_column(Integer)
    retained_runtime_session_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    recovery_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    runtime_session_cleanup_status: Mapped[RuntimeSessionCleanupStatus] = (
        mapped_column(
            enum_type(
                RuntimeSessionCleanupStatus, "runtime_session_cleanup_status"
            ),
            nullable=False,
            default=RuntimeSessionCleanupStatus.NOT_REQUIRED,
        )
    )
    runtime_abort_status: Mapped[RuntimeAbortStatus] = mapped_column(
        enum_type(RuntimeAbortStatus, "runtime_abort_status"),
        nullable=False,
        default=RuntimeAbortStatus.NOT_REQUIRED,
    )
    finalization_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    active_operation_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True, index=True
    )
    operation_wait_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    execution_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    traceparent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    tracestate: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
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
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

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
            operation_mode=execution.operation_mode,
            operation_wait_timeout_seconds=execution.operation_wait_timeout_seconds,
            trigger_type=execution.trigger_type,
            runtime_type=execution.runtime_type,
            runtime_pool=execution.runtime_pool,
            runtime_profile=execution.runtime_profile,
            user_id=execution.user_id,
            project_id=execution.project_id,
            session_id=execution.session_id,
            task_id=execution.task_id,
            workflow_id=execution.workflow_id,
            created_by_type=execution.created_by_type,
            created_by=execution.created_by,
            updated_by_type=execution.updated_by_type,
            updated_by=execution.updated_by,
            execution_metadata=execution.metadata,
            cancellation_reason=execution.cancellation_reason,
            runtime_target_id=execution.runtime_target_id,
            runtime_session_id=execution.runtime_session_id,
            workspace_path=execution.workspace_path,
            notebook_path=execution.notebook_path,
            notebook_projection_status=(execution.notebook_projection_status),
            notebook_projection_attempt_count=(
                execution.notebook_projection_attempt_count
            ),
            notebook_projection_error=execution.notebook_projection_error,
            notebook_projected_at=execution.notebook_projected_at,
            error_message=execution.error_message,
            failure_type=execution.failure_type,
            lease_owner=execution.lease_owner,
            lease_expires_at=execution.lease_expires_at,
            heartbeat_at=execution.heartbeat_at,
            retry_strategy=execution.retry_strategy,
            retry_from_sequence=execution.retry_from_sequence,
            retained_runtime_session_until=execution.retained_runtime_session_until,
            retry_count=execution.retry_count,
            recovery_count=execution.recovery_count,
            runtime_session_cleanup_status=execution.runtime_session_cleanup_status,
            runtime_abort_status=execution.runtime_abort_status,
            finalization_requested=execution.finalization_requested,
            active_operation_id=execution.active_operation_id,
            operation_wait_expires_at=execution.operation_wait_expires_at,
            execution_expires_at=execution.execution_expires_at,
            traceparent=execution.traceparent,
            tracestate=execution.tracestate,
            version=execution.version,
            created_at=execution.created_at,
            updated_at=execution.updated_at,
            started_at=execution.started_at,
            finished_at=execution.finished_at,
            steps=[
                ExecutionStepORM.from_domain(step) for step in execution.steps
            ],
        )

    def to_domain(self) -> Execution:
        return Execution(
            id=self.id,
            idempotency_key=self.idempotency_key,
            request_fingerprint=self.request_fingerprint,
            cancel_idempotency_key=self.cancel_idempotency_key,
            status=self.status,
            operation_mode=self.operation_mode,
            operation_wait_timeout_seconds=self.operation_wait_timeout_seconds,
            trigger_type=self.trigger_type,
            runtime_type=self.runtime_type,
            runtime_pool=self.runtime_pool,
            runtime_profile=self.runtime_profile,
            user_id=self.user_id,
            project_id=self.project_id,
            session_id=self.session_id,
            task_id=self.task_id,
            workflow_id=self.workflow_id,
            created_by_type=self.created_by_type,
            created_by=self.created_by,
            updated_by_type=self.updated_by_type,
            updated_by=self.updated_by,
            metadata=self.execution_metadata,
            cancellation_reason=self.cancellation_reason,
            runtime_target_id=self.runtime_target_id,
            runtime_session_id=self.runtime_session_id,
            workspace_path=self.workspace_path,
            notebook_path=self.notebook_path,
            notebook_projection_status=self.notebook_projection_status,
            notebook_projection_attempt_count=(
                self.notebook_projection_attempt_count
            ),
            notebook_projection_error=self.notebook_projection_error,
            notebook_projected_at=self.notebook_projected_at,
            error_message=self.error_message,
            failure_type=self.failure_type,
            lease_owner=self.lease_owner,
            lease_expires_at=self.lease_expires_at,
            heartbeat_at=self.heartbeat_at,
            retry_strategy=self.retry_strategy,
            retry_from_sequence=self.retry_from_sequence,
            retained_runtime_session_until=self.retained_runtime_session_until,
            retry_count=self.retry_count,
            recovery_count=self.recovery_count,
            runtime_session_cleanup_status=self.runtime_session_cleanup_status,
            runtime_abort_status=self.runtime_abort_status,
            finalization_requested=self.finalization_requested,
            active_operation_id=self.active_operation_id,
            operation_wait_expires_at=self.operation_wait_expires_at,
            execution_expires_at=self.execution_expires_at,
            traceparent=self.traceparent,
            tracestate=self.tracestate,
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
        *audit_actor_constraints(),
        UniqueConstraint(
            "execution_id",
            "sequence",
            name="uq_execution_steps_execution_sequence",
        ),
        CheckConstraint("sequence >= 0", name="non_negative_sequence"),
        CheckConstraint(
            "step_timeout_seconds IS NULL OR step_timeout_seconds >= 1",
            name="valid_step_timeout",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'SKIPPED', 'CANCELLED')",
            name="valid_step_status",
        ),
        CheckConstraint(
            "source_type IN ('INLINE', 'PATH')", name="valid_source_type"
        ),
        CheckConstraint(
            "(source_type = 'INLINE' AND source_path IS NULL) OR "
            "(source_type = 'PATH' AND source_path IS NOT NULL)",
            name="valid_source",
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
    operation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("execution_operations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    source_type: Mapped[CodeSourceType] = mapped_column(
        enum_type(CodeSourceType, "step_source_type"), nullable=False
    )
    source_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_snapshot_path: Mapped[str] = mapped_column(Text, nullable=False)
    source_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    code_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    step_timeout_seconds: Mapped[int | None] = mapped_column(Integer)
    skill_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[StepStatus] = mapped_column(
        enum_type(StepStatus, "step_status"),
        nullable=False,
        default=StepStatus.PENDING,
    )
    input_parameters: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    output_summary: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=empty_output_summary
    )
    result_execution_attempt_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("execution_attempts.id", ondelete="SET NULL"),
        nullable=True,
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
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    execution: Mapped[ExecutionORM] = relationship(back_populates="steps")

    @classmethod
    def from_domain(cls, step: ExecutionStep) -> "ExecutionStepORM":
        return cls(
            id=step.id,
            operation_id=step.operation_id,
            sequence=step.sequence,
            source_type=step.source_type,
            source_path=step.source_path,
            source_sha256=step.source_sha256,
            source_snapshot_path=step.source_snapshot_path,
            source_size_bytes=step.source_size_bytes,
            code_hash=step.code_hash,
            step_timeout_seconds=step.step_timeout_seconds,
            skill_name=step.skill_name,
            tool_name=step.tool_name,
            status=step.status,
            input_parameters=step.input_parameters,
            output_summary=step.output_summary,
            result_execution_attempt_id=step.result_execution_attempt_id,
            result_manifest_path=step.result_manifest_path,
            result_manifest_checksum_sha256=(
                step.result_manifest_checksum_sha256
            ),
            result_manifest_size_bytes=step.result_manifest_size_bytes,
            result_fencing_token=step.result_fencing_token,
            result_complete=step.result_complete,
            result_representation_count=step.result_representation_count,
            result_total_size_bytes=step.result_total_size_bytes,
            error_message=step.error_message,
            created_by_type=step.created_by_type,
            created_by=step.created_by,
            updated_by_type=step.updated_by_type,
            updated_by=step.updated_by,
            created_at=step.created_at,
            updated_at=step.updated_at,
            started_at=step.started_at,
            finished_at=step.finished_at,
        )

    def to_domain(self) -> ExecutionStep:
        return ExecutionStep(
            id=self.id,
            operation_id=self.operation_id,
            sequence=self.sequence,
            code="",
            source_type=self.source_type,
            source_path=self.source_path,
            source_sha256=self.source_sha256,
            source_snapshot_path=self.source_snapshot_path,
            source_size_bytes=self.source_size_bytes,
            code_hash=self.code_hash,
            step_timeout_seconds=self.step_timeout_seconds,
            skill_name=self.skill_name,
            tool_name=self.tool_name,
            status=self.status,
            input_parameters=self.input_parameters,
            output_summary=self.output_summary,
            result_execution_attempt_id=self.result_execution_attempt_id,
            result_manifest_path=self.result_manifest_path,
            result_manifest_checksum_sha256=(
                self.result_manifest_checksum_sha256
            ),
            result_manifest_size_bytes=self.result_manifest_size_bytes,
            result_fencing_token=self.result_fencing_token,
            result_complete=self.result_complete,
            result_representation_count=self.result_representation_count,
            result_total_size_bytes=self.result_total_size_bytes,
            error_message=self.error_message,
            created_by_type=self.created_by_type,
            created_by=self.created_by,
            updated_by_type=self.updated_by_type,
            updated_by=self.updated_by,
            created_at=self.created_at,
            updated_at=self.updated_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
        )


class ExecutionOperationORM(Base):
    """One Agent-submitted Step batch with immutable provenance and mutable processing state."""

    __tablename__ = "execution_operations"
    __table_args__ = (
        *audit_actor_constraints(),
        UniqueConstraint(
            "execution_id",
            "operation_number",
            name="uq_operations_execution_number",
        ),
        CheckConstraint(
            "operation_number > 0", name="positive_operation_number"
        ),
        CheckConstraint(
            "schema_version = '1.0'", name="supported_schema_version"
        ),
        CheckConstraint(
            "first_sequence >= 0", name="non_negative_first_sequence"
        ),
        CheckConstraint(
            "last_sequence >= first_sequence",
            name="valid_operation_sequence_range",
        ),
        CheckConstraint(
            "operation_timeout_seconds IS NULL OR operation_timeout_seconds >= 1",
            name="valid_operation_timeout",
        ),
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name="valid_operation_status",
        ),
        Index(
            "ix_execution_operations_execution_number",
            "execution_id",
            "operation_number",
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
    operation_number: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(
        String(16), nullable=False, default="1.0"
    )
    first_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    last_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    operation_timeout_seconds: Mapped[int | None] = mapped_column(Integer)
    operation_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True
    )
    request_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    status: Mapped[OperationStatus] = mapped_column(
        enum_type(OperationStatus, "operation_status"), nullable=False
    )
    execution_attempt_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("execution_attempts.id", use_alter=True),
        nullable=True,
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
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    @classmethod
    def from_domain(
        cls, operation: ExecutionOperation
    ) -> "ExecutionOperationORM":
        return cls(
            id=operation.id,
            execution_id=operation.execution_id,
            operation_number=operation.operation_number,
            schema_version=operation.schema_version,
            first_sequence=operation.first_sequence,
            last_sequence=operation.last_sequence,
            operation_timeout_seconds=operation.operation_timeout_seconds,
            operation_metadata=operation.metadata,
            idempotency_key=operation.idempotency_key,
            request_fingerprint=operation.request_fingerprint,
            status=operation.status,
            execution_attempt_id=operation.execution_attempt_id,
            error_message=operation.error_message,
            created_by_type=operation.created_by_type,
            created_by=operation.created_by,
            updated_by_type=operation.updated_by_type,
            updated_by=operation.updated_by,
            created_at=operation.created_at,
            updated_at=operation.updated_at,
            started_at=operation.started_at,
            finished_at=operation.finished_at,
        )


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
            "'RUNTIME_SESSION_LOST')",
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


class ExecutionArtifactORM(Base):
    """Artifact evidence attached at Execution, Attempt, or Step scope."""

    __tablename__ = "execution_artifacts"
    __table_args__ = (
        *audit_actor_constraints(),
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
        CheckConstraint(
            "size_bytes IS NULL OR size_bytes >= 0", name="non_negative_size"
        ),
        Index(
            "ix_execution_artifacts_execution_created",
            "execution_id",
            "created_at",
            "id",
        ),
        Index(
            "ix_execution_artifacts_step", "execution_step_id", "created_at"
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
    execution_attempt_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("execution_attempts.id", ondelete="CASCADE"),
        nullable=True,
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
        Uuid(as_uuid=True),
        ForeignKey("execution_artifacts.id", ondelete="SET NULL"),
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
    identity_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
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


class ExecutionEventSequenceORM(Base):
    __tablename__ = "execution_event_sequences"
    __table_args__ = (
        CheckConstraint(
            "last_sequence >= 1",
            name="positive_last_sequence",
        ),
    )

    execution_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("executions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    last_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)


class ExecutionEventORM(Base):
    __tablename__ = "execution_events"
    __table_args__ = (
        *audit_actor_constraints(),
        CheckConstraint(
            "event_sequence >= 1",
            name="positive_event_sequence",
        ),
        UniqueConstraint(
            "execution_id",
            "event_sequence",
            name="uq_execution_events_execution_sequence",
        ),
        Index(
            "ix_execution_events_execution_cursor",
            "execution_id",
            "event_sequence",
        ),
        Index("ix_execution_events_created", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    execution_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("executions.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(255), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_by_type: Mapped[ActorType | None] = mapped_column(
        enum_type(ActorType, "actor_type"), nullable=True
    )
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_by_type: Mapped[ActorType | None] = mapped_column(
        enum_type(ActorType, "actor_type"), nullable=True
    )
    updated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    traceparent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    tracestate: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    @classmethod
    def from_domain(cls, event: ExecutionEvent) -> "ExecutionEventORM":
        return cls(
            id=event.id,
            execution_id=event.execution_id,
            event_sequence=event.event_sequence,
            event_type=event.event_type,
            schema_version=event.schema_version,
            payload=event.payload,
            created_by_type=event.created_by_type,
            created_by=event.created_by,
            updated_by_type=event.updated_by_type,
            updated_by=event.updated_by,
            traceparent=event.traceparent,
            tracestate=event.tracestate,
            created_at=event.created_at,
            updated_at=event.updated_at,
        )


class OutboxEventORM(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        *audit_actor_constraints(),
        CheckConstraint(
            "status IN ('PENDING', 'PUBLISHED')", name="valid_outbox_status"
        ),
        CheckConstraint(
            "destination IN ('WORK', 'EVENTS')",
            name="valid_outbox_destination",
        ),
        CheckConstraint(
            "(destination = 'EVENTS' AND event_sequence >= 1 "
            "AND execution_event_id IS NOT NULL AND payload IS NULL) OR "
            "(destination = 'WORK' AND event_sequence IS NULL "
            "AND execution_event_id IS NULL AND payload IS NOT NULL)",
            name="valid_outbox_content",
        ),
        UniqueConstraint(
            "aggregate_type",
            "aggregate_id",
            "destination",
            "event_sequence",
            name="uq_outbox_aggregate_event_sequence",
        ),
        UniqueConstraint(
            "execution_event_id",
            name="uq_outbox_execution_event_id",
        ),
        Index("ix_outbox_pending", "status", "available_at", "created_at"),
        Index(
            "ix_outbox_pending_event_order",
            "aggregate_type",
            "aggregate_id",
            "event_sequence",
            postgresql_where=text(
                "destination = 'EVENTS' AND status = 'PENDING'"
            ),
            sqlite_where=text("destination = 'EVENTS' AND status = 'PENDING'"),
        ),
        Index(
            "ix_outbox_execution_cursor",
            "aggregate_type",
            "aggregate_id",
            "event_sequence",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    aggregate_type: Mapped[str] = mapped_column(String(128), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(255), nullable=False)
    event_sequence: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    execution_event_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("execution_events.id", ondelete="RESTRICT"),
        nullable=True,
    )
    destination: Mapped[OutboxDestination] = mapped_column(
        enum_type(OutboxDestination, "outbox_destination"), nullable=False
    )
    payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    created_by_type: Mapped[ActorType | None] = mapped_column(
        enum_type(ActorType, "actor_type"), nullable=True
    )
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_by_type: Mapped[ActorType | None] = mapped_column(
        enum_type(ActorType, "actor_type"), nullable=True
    )
    updated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    traceparent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    tracestate: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[OutboxStatus] = mapped_column(
        enum_type(OutboxStatus, "outbox_status"), nullable=False
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, onupdate=utc_now
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    execution_event: Mapped[ExecutionEventORM | None] = relationship(
        lazy="selectin"
    )

    @classmethod
    def from_domain(cls, event: OutboxEvent) -> "OutboxEventORM":
        return cls(
            id=event.id,
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            event_type=event.event_type,
            event_sequence=event.event_sequence,
            execution_event_id=None,
            destination=event.destination,
            payload=event.payload,
            created_by_type=event.created_by_type,
            created_by=event.created_by,
            updated_by_type=event.updated_by_type,
            updated_by=event.updated_by,
            traceparent=event.traceparent,
            tracestate=event.tracestate,
            status=event.status,
            attempt_count=event.attempt_count,
            available_at=event.available_at,
            created_at=event.created_at,
            updated_at=event.updated_at,
            published_at=event.published_at,
            last_error=event.last_error,
        )

    @classmethod
    def from_execution_event(cls, event: ExecutionEvent) -> "OutboxEventORM":
        return cls(
            aggregate_type="Execution",
            aggregate_id=event.execution_id,
            event_type=event.event_type,
            event_sequence=event.event_sequence,
            execution_event_id=event.id,
            destination=OutboxDestination.EVENTS,
            payload=None,
            created_by_type=event.created_by_type,
            created_by=event.created_by,
            updated_by_type=event.updated_by_type,
            updated_by=event.updated_by,
            traceparent=event.traceparent,
            tracestate=event.tracestate,
            status=OutboxStatus.PENDING,
            attempt_count=0,
            available_at=event.created_at,
            created_at=event.created_at,
            updated_at=event.updated_at,
        )
