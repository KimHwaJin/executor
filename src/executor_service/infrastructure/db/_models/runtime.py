"""Runtime Target SQLAlchemy ORM models."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from executor_service.domain.enums import (
    ActorType,
    RuntimePool,
    RuntimeTargetStatus,
    RuntimeType,
)
from executor_service.domain.models import utc_now
from executor_service.infrastructure.db._models.common import (
    audit_actor_constraints,
    enum_type,
)
from executor_service.infrastructure.db.base import Base


class RuntimeTargetORM(Base):
    __tablename__ = "runtime_targets"
    __table_args__ = (
        *audit_actor_constraints(),
        CheckConstraint(
            "status IN ('ACTIVE', 'DRAINING', 'OFFLINE')",
            name="valid_runtime_target_status",
        ),
        CheckConstraint(
            "runtime_type IN ('JUPYTER')", name="valid_runtime_type"
        ),
        CheckConstraint(
            "max_concurrent_executions > 0", name="positive_max_concurrency"
        ),
        CheckConstraint(
            "active_session_count IS NULL OR active_session_count >= 0",
            name="non_negative_active_session_count",
        ),
        Index("ix_runtime_targets_pool_status", "pool", "enabled", "status"),
        Index("ix_runtime_targets_created_cursor", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    runtime_type: Mapped[RuntimeType] = mapped_column(
        enum_type(RuntimeType, "runtime_target_type"),
        nullable=False,
        default=RuntimeType.JUPYTER,
    )
    connection_config: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False
    )
    credential_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    credential_ciphertext: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    pool: Mapped[RuntimePool] = mapped_column(
        enum_type(RuntimePool, "runtime_target_pool"), nullable=False
    )
    status: Mapped[RuntimeTargetStatus] = mapped_column(
        enum_type(RuntimeTargetStatus, "runtime_target_status"),
        nullable=False,
    )
    max_concurrent_executions: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    supported_profiles: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    last_health_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_health_error: Mapped[str | None] = mapped_column(String(500))
    active_session_count: Mapped[int | None] = mapped_column(Integer)
    session_count_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    resource_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    resource_last_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    resource_last_error: Mapped[str | None] = mapped_column(String(500))
    resource_source: Mapped[str | None] = mapped_column(String(64))
    resource_estimated: Mapped[bool | None] = mapped_column(Boolean)
    resource_process_count: Mapped[int | None] = mapped_column(Integer)
    cpu_used_cores: Mapped[float | None] = mapped_column(Float)
    cpu_capacity_cores: Mapped[float | None] = mapped_column(Float)
    cpu_utilization: Mapped[float | None] = mapped_column(Float)
    memory_used_bytes: Mapped[int | None] = mapped_column(BigInteger)
    memory_capacity_bytes: Mapped[int | None] = mapped_column(BigInteger)
    memory_utilization: Mapped[float | None] = mapped_column(Float)
    resource_errors: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
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
        DateTime(timezone=True), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class RuntimeTargetPurgeORM(Base):
    """Immutable audit tombstone for a physically removed target."""

    __tablename__ = "runtime_target_purges"
    __table_args__ = (
        *audit_actor_constraints(),
        CheckConstraint("pool IN ('INTERACTIVE', 'BATCH')", name="valid_pool"),
        CheckConstraint(
            "runtime_type IN ('JUPYTER')", name="valid_runtime_type"
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    target_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, unique=True
    )
    target_name: Mapped[str] = mapped_column(String(255), nullable=False)
    runtime_type: Mapped[RuntimeType] = mapped_column(
        enum_type(RuntimeType, "runtime_target_purge_type"), nullable=False
    )
    connection_config: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False
    )
    pool: Mapped[RuntimePool] = mapped_column(
        enum_type(RuntimePool, "runtime_target_purge_pool"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True
    )
    request_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False
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
