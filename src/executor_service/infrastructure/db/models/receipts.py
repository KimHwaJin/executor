"""Shared idempotency receipt SQLAlchemy ORM model."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from executor_service.domain.models import utc_now
from executor_service.infrastructure.db.base import Base
from executor_service.infrastructure.db.models.common import UTCDateTime

# Operator commands (Runtime Targets, maintenance) share one key space.
GLOBAL_RECEIPT_SCOPE = ""


def execution_receipt_scope(execution_id: UUID) -> str:
    """Keys of commands on one Execution never collide with other callers'."""
    return f"execution:{execution_id}"


class CommandReceiptORM(Base):
    """Idempotency receipt shared by non-execution mutating commands."""

    __tablename__ = "command_receipts"
    __table_args__ = (
        UniqueConstraint(
            "scope",
            "idempotency_key",
            name="uq_command_receipts_scope_idempotency_key",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    scope: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default=GLOBAL_RECEIPT_SCOPE,
        server_default=GLOBAL_RECEIPT_SCOPE,
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    command_type: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    result: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
