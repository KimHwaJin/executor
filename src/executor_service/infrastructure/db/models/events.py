"""Durable execution event and outbox SQLAlchemy ORM models."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from executor_service.domain.enums import (
    ActorType,
    OutboxStatus,
)
from executor_service.domain.models import (
    ExecutionEvent,
    utc_now,
)
from executor_service.infrastructure.db.base import Base
from executor_service.infrastructure.db.models.common import (
    audit_actor_constraints,
    enum_type,
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
        CheckConstraint("event_sequence >= 1", name="valid_outbox_sequence"),
        UniqueConstraint(
            "aggregate_type",
            "aggregate_id",
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
            postgresql_where=text("status = 'PENDING'"),
            sqlite_where=text("status = 'PENDING'"),
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
    event_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    execution_event_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("execution_events.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_by_type: Mapped[ActorType | None] = mapped_column(
        enum_type(ActorType, "actor_type"), nullable=True
    )
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_by_type: Mapped[ActorType | None] = mapped_column(
        enum_type(ActorType, "actor_type"), nullable=True
    )
    updated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
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
    execution_event: Mapped[ExecutionEventORM] = relationship(lazy="selectin")

    @classmethod
    def from_execution_event(cls, event: ExecutionEvent) -> "OutboxEventORM":
        return cls(
            aggregate_type="Execution",
            aggregate_id=event.execution_id,
            event_type=event.event_type,
            event_sequence=event.event_sequence,
            execution_event_id=event.id,
            created_by_type=event.created_by_type,
            created_by=event.created_by,
            updated_by_type=event.updated_by_type,
            updated_by=event.updated_by,
            status=OutboxStatus.PENDING,
            attempt_count=0,
            available_at=event.created_at,
            created_at=event.created_at,
            updated_at=event.updated_at,
        )
