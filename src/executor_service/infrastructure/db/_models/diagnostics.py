"""Append-only, bounded failure evidence. Execution deletion owns retention."""

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
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from executor_service.domain.enums import ActorType
from executor_service.infrastructure.db._models.common import (
    audit_actor_constraints,
    enum_type,
)
from executor_service.infrastructure.db.base import Base


class ExecutionDiagnosticORM(Base):
    __tablename__ = "execution_diagnostics"
    __table_args__ = (
        *audit_actor_constraints(),
        CheckConstraint("fencing_token >= 1", name="positive_fencing_token"),
        CheckConstraint(
            "step_sequence IS NULL OR step_sequence >= 0",
            name="nonnegative_step_sequence",
        ),
        Index(
            "ix_diagnostics_execution_cursor",
            "execution_id",
            "created_at",
            "id",
        ),
        Index(
            "ix_diagnostics_attempt_cursor",
            "execution_id",
            "attempt_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    execution_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("executions.id", ondelete="CASCADE"), nullable=False
    )
    # Immutable scope snapshots; the writer validates ownership under fencing.
    attempt_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    operation_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    step_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    step_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_by_type: Mapped[ActorType | None] = mapped_column(
        enum_type(ActorType, "actor_type"), nullable=True
    )
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_by_type: Mapped[ActorType | None] = mapped_column(
        enum_type(ActorType, "actor_type"), nullable=True
    )
    updated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
