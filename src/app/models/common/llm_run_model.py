from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import LLMRunStatus, enum_values
from app.models.model_base import Base


class LLMRunModel(Base):
    """LLM 요청, 원본 응답, 토큰 및 처리 상태를 보존하는 실행 로그."""

    __tablename__ = "llm_runs"

    run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sessions.session_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    trigger_message_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("messages.message_id", ondelete="RESTRICT"),
        nullable=False,
    )
    assistant_message_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("messages.message_id", ondelete="CASCADE"),
        nullable=True,
    )

    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    temperature: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    max_output_tokens: Mapped[int | None] = mapped_column(Integer)

    project_prompt_version: Mapped[int] = mapped_column(Integer, nullable=False)
    system_prompt_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    request_messages_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    raw_response: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")

    status: Mapped[LLMRunStatus] = mapped_column(
        Enum(
            LLMRunStatus,
            name="llm_run_status",
            values_callable=enum_values,
        ),
        nullable=False,
        default=LLMRunStatus.QUEUED,
        server_default=LLMRunStatus.QUEUED.value,
    )
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    finish_reason: Mapped[str | None] = mapped_column(String(100))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=5, server_default="5")
    attempt_errors: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )

    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("trigger_message_id", name="uq_llm_runs_trigger_message"),
        UniqueConstraint("assistant_message_id", name="uq_llm_runs_assistant_message"),
    )

    session = relationship("SessionModel", back_populates="llm_runs")
    trigger_message = relationship(
        "MessageModel",
        back_populates="trigger_llm_runs",
        foreign_keys=[trigger_message_id],
    )
    assistant_message = relationship(
        "MessageModel",
        back_populates="assistant_llm_runs",
        foreign_keys=[assistant_message_id],
    )

