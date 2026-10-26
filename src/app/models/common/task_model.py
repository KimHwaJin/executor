from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Index, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import TaskStatus, enum_values
from app.models.model_base import Base


class TaskModel(Base):
    """E03 분석 Task와 Session 활성 잠금을 관리합니다. Agent 실행 로그와 분리합니다."""

    __tablename__ = "tasks"

    task_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    # LangGraph가 분석 단위로 생성하고 Executor source 경로에 사용하는 별도 ID입니다.
    graph_task_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), unique=True, index=True
    )
    # 최초 분석 Run과 LangGraph checkpoint 기준 Run입니다. Task는 여러 AgentRun을 가집니다.
    root_run_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agent_runs.run_id", ondelete="SET NULL"),
        index=True,
    )
    checkpoint_run_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agent_runs.run_id", ondelete="SET NULL"),
        index=True,
    )
    session_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sessions.session_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    trigger_message_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("messages.message_id", ondelete="RESTRICT")
    )
    trigger_type: Mapped[str] = mapped_column(String(30), nullable=False, default="pending_route")
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, name="task_status", values_callable=enum_values, create_type=False),
        nullable=False,
        default=TaskStatus.RUNNING,
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    lock_owner: Mapped[str | None] = mapped_column(String(255))
    lock_token: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    # Worker가 요청을 확인하고 실제 실행을 중단할 때까지 status/lease는 active로 유지합니다.
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    # E05-T05: Task 내부 SSE event sequence를 DB row lock 아래 원자적으로 증가시킵니다.
    last_event_sequence: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("session_id", "idempotency_key", name="uq_tasks_session_idempotency"),
        # E03-T03: DB가 Session당 활성 분석 Task 하나만 허용합니다.
        Index(
            "uq_tasks_session_active",
            "session_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
    )

    agent_runs = relationship(
        "AgentRunModel",
        back_populates="task",
        foreign_keys="AgentRunModel.task_id",
        order_by="AgentRunModel.created_at",
    )
    root_run = relationship("AgentRunModel", foreign_keys=[root_run_id], post_update=True)
    checkpoint_run = relationship("AgentRunModel", foreign_keys=[checkpoint_run_id], post_update=True)
    session = relationship("SessionModel", back_populates="tasks")
    events = relationship("TaskEventModel", back_populates="task", cascade="all, delete-orphan")
