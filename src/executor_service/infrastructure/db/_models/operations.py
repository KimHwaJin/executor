"""Execution operation SQLAlchemy ORM model."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
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

from executor_service.domain.enums import ActorType, OperationStatus
from executor_service.domain.models import ExecutionOperation, utc_now
from executor_service.infrastructure.db._models.common import (
    audit_actor_constraints,
    enum_type,
)
from executor_service.infrastructure.db.base import Base


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
