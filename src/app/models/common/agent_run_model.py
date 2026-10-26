from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Enum, ForeignKey, ForeignKeyConstraint, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import AgentRunStatus, enum_values
from app.models.model_base import Base


class AgentRunModel(Base):
    """A single Agent graph invocation; never reused when an interrupt resumes."""

    __tablename__ = "agent_runs"

    run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("sessions.session_id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 하나의 장기 분석 Task 아래 최초 호출과 모든 HITL resume Run을 묶습니다.
    # tasks.root_run_id/checkpoint_run_id와 순환 FK이므로 CREATE 후 ALTER로 추가합니다.
    task_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), index=True)
    trigger_message_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("messages.message_id", ondelete="RESTRICT"), index=True
    )
    agent_message_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("messages.message_id", ondelete="SET NULL")
    )
    interpreted_message_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("messages.message_id", ondelete="SET NULL")
    )
    workflow_stage: Mapped[str] = mapped_column(
        String(30), nullable=False, default="prepared", server_default="prepared", index=True
    )
    request_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    agent_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    redis_key: Mapped[str | None] = mapped_column(String(500), index=True)
    redis_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[AgentRunStatus] = mapped_column(
        Enum(AgentRunStatus, name="agent_run_status", values_callable=enum_values, create_type=False),
        nullable=False, default=AgentRunStatus.PENDING, server_default=AgentRunStatus.PENDING.value,
    )
    input_json: Mapped[dict[str, Any] | None] = mapped_column("input", JSONB)
    # JSONB에는 객체형 HITL 응답과 로컬 "mock" shortcut을 모두 보존합니다.
    command_json: Mapped[dict[str, Any] | str | None] = mapped_column("command", JSONB)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, default=dict, server_default="{}")
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    multitask_strategy: Mapped[str] = mapped_column(String(30), nullable=False, default="reject", server_default="reject")
    stream_mode: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    stream_resumable: Mapped[bool] = mapped_column(nullable=False, default=True, server_default="true")
    on_disconnect: Mapped[str] = mapped_column(String(30), nullable=False, default="continue", server_default="continue")
    # LangGraph는 한 시점에 여러 interrupt를 반환할 수 있으므로 JSON 배열로 보존합니다.
    interrupt: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    failure: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Worker가 실제 실행을 점유한 횟수입니다. 최초 실행도 1회로 계산합니다.
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # 지수 backoff가 끝나기 전에는 다른 Pod도 이 Run을 점유하지 않습니다.
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    # 취소 요청과 실제 종료를 분리해 실행이 멈추기 전에 Session lock이 풀리지 않게 합니다.
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    agent_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    redis_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    interpreted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # E03-T03: 동일 요청 재전송은 DB에서 하나의 Run으로 수렴시킵니다.
        UniqueConstraint(
            "session_id",
            "idempotency_key",
            name="uq_agent_runs_session_idempotency",
        ),
        ForeignKeyConstraint(
            ["task_id"],
            ["tasks.task_id"],
            name="fk_agent_runs_task",
            ondelete="SET NULL",
            use_alter=True,
        ),
    )

    session = relationship("SessionModel", back_populates="agent_runs")
    task = relationship("TaskModel", back_populates="agent_runs", foreign_keys=[task_id])
    logs = relationship("AgentRunLogModel", back_populates="agent_run")

    @property
    def checkpoint_run_id(self) -> UUID:
        """HITL resume가 이어갈 최초 LangGraph thread의 Run ID입니다."""
        value = (self.metadata_json or {}).get("checkpoint_run_id")
        return UUID(str(value)) if value else self.run_id

