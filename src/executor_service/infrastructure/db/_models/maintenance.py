"""Executor maintenance and retention SQLAlchemy ORM models."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from executor_service.domain.enums import (
    ActorType,
    ExecutionStatus,
    ExecutorAdmissionState,
    MaintenanceRunAction,
    MaintenanceRunStatus,
    MaintenanceRunTargetStatus,
)
from executor_service.domain.models import utc_now
from executor_service.infrastructure.db._models.common import (
    audit_actor_constraints,
    enum_type,
)
from executor_service.infrastructure.db.base import Base


class ExecutorMaintenanceORM(Base):
    """Singleton state shared by every Executor Worker replica."""

    __tablename__ = "executor_maintenance"
    __table_args__ = (
        *audit_actor_constraints(),
        CheckConstraint("singleton_key = 'executor'", name="singleton_key"),
        CheckConstraint(
            "admission_state IN ('ACTIVE', 'DRAINING')",
            name="valid_admission_state",
        ),
        CheckConstraint("version >= 0", name="non_negative_version"),
    )

    singleton_key: Mapped[str] = mapped_column(
        String(32), primary_key=True, default="executor"
    )
    admission_state: Mapped[ExecutorAdmissionState] = mapped_column(
        enum_type(ExecutorAdmissionState, "executor_admission_state"),
        nullable=False,
        default=ExecutorAdmissionState.ACTIVE,
    )
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
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


class EventRetentionLeaseORM(Base):
    __tablename__ = "event_retention_lease"
    __table_args__ = (
        CheckConstraint(
            "(lease_owner IS NULL) = (lease_expires_at IS NULL)",
            name="complete_lease",
        ),
    )

    singleton_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    lease_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )


class MaintenanceRunORM(Base):
    __tablename__ = "maintenance_runs"
    __table_args__ = (
        *audit_actor_constraints(),
        CheckConstraint(
            "action IN ('STOP_ACTIVE_EXECUTIONS')", name="valid_action"
        ),
        CheckConstraint(
            "status IN ('REQUESTED', 'RUNNING', 'SUCCEEDED', 'FAILED')",
            name="valid_status",
        ),
        CheckConstraint(
            "fencing_token >= 0", name="non_negative_fencing_token"
        ),
        Index("ix_maintenance_runs_recovery", "status", "lease_expires_at"),
        Index("ix_maintenance_runs_created", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True
    )
    request_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    action: Mapped[MaintenanceRunAction] = mapped_column(
        enum_type(MaintenanceRunAction, "maintenance_run_action"),
        nullable=False,
    )
    status: Mapped[MaintenanceRunStatus] = mapped_column(
        enum_type(MaintenanceRunStatus, "maintenance_run_status"),
        nullable=False,
    )
    lease_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    fencing_token: Mapped[int] = mapped_column(
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
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class MaintenanceRunTargetORM(Base):
    __tablename__ = "maintenance_run_targets"
    __table_args__ = (
        *audit_actor_constraints(),
        UniqueConstraint(
            "maintenance_run_id",
            "execution_id",
            name="uq_maintenance_run_targets_run_execution",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'STOP_REQUESTED', 'STOPPED', 'FAILED')",
            name="valid_status",
        ),
        Index(
            "ix_maintenance_run_targets_run_status",
            "maintenance_run_id",
            "status",
        ),
        Index(
            "ix_maintenance_run_targets_cursor",
            "maintenance_run_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    maintenance_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("maintenance_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    execution_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("executions.id"),
        nullable=False,
    )
    selected_execution_status: Mapped[ExecutionStatus] = mapped_column(
        enum_type(ExecutionStatus, "maintenance_selected_execution_status"),
        nullable=False,
    )
    status: Mapped[MaintenanceRunTargetStatus] = mapped_column(
        enum_type(
            MaintenanceRunTargetStatus,
            "maintenance_run_target_status",
        ),
        nullable=False,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    stop_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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
