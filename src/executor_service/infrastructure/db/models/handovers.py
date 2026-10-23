"""Jupyter journal requests and Step handover between Workers.

Kept in their own tables so Workers on the previous schema never read or
write them during a rolling update.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import JSON, DateTime, ForeignKey, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from executor_service.domain.enums import HandoverReason
from executor_service.domain.models import utc_now
from executor_service.infrastructure.db.base import Base
from executor_service.infrastructure.db.models.common import enum_type


class ExecutionStepRunORM(Base):
    """The journal request of one Step run; code is sent at most once."""

    __tablename__ = "execution_step_runs"

    execution_step_attempt_id: Mapped[UUID] = mapped_column(
        Uuid(),
        ForeignKey("execution_step_attempts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    execution_id: Mapped[UUID] = mapped_column(
        Uuid(),
        ForeignKey("executions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    request_id: Mapped[UUID] = mapped_column(
        Uuid(), nullable=False, unique=True
    )
    artifact_baseline: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class ExecutionHandoverORM(Base):
    """A running Step released for another Worker to follow."""

    __tablename__ = "execution_handovers"

    execution_id: Mapped[UUID] = mapped_column(
        Uuid(),
        ForeignKey("executions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    reason: Mapped[HandoverReason] = mapped_column(
        enum_type(HandoverReason, "handover_reason"), nullable=False
    )
    # When output delivery stopped: the last heartbeat after a crash.
    paused_since: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
