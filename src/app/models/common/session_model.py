from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import DeleteYN, enum_values
from app.models.model_base import Base, TimestampMixin


class SessionModel(TimestampMixin, Base):
    """Project 내부의 개별 채팅방."""

    __tablename__ = "sessions"

    session_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    project_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("projects.project_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    session_name: Mapped[str] = mapped_column(
        String(300),
        nullable=False,
        default="새 대화",
        server_default="새 대화",
    )

    current_leaf_message_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
    )

    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )

    delete_yn: Mapped[DeleteYN] = mapped_column(
        Enum(
            DeleteYN,
            name="delete_yn",
            values_callable=enum_values,
            create_type=False,
        ),
        nullable=False,
        default=DeleteYN.N,
        server_default=DeleteYN.N.value,
    )

    __table_args__ = (
        # current leaf가 반드시 같은 Session의 Message를 가리키게 합니다.
        ForeignKeyConstraint(
            ["current_leaf_message_id", "session_id"],
            ["messages.message_id", "messages.session_id"],
            name="fk_sessions_current_leaf",
            use_alter=True,
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    project = relationship(
        "ProjectModel",
        back_populates="sessions",
        foreign_keys=[project_id],
    )
    user = relationship(
        "UserModel",
        back_populates="sessions",
        foreign_keys=[user_id],
    )
    messages = relationship(
        "MessageModel",
        back_populates="session",
        foreign_keys="MessageModel.session_id",
    )
    llm_runs = relationship("LLMRunModel", back_populates="session")
    agent_runs = relationship("AgentRunModel", back_populates="session")
    tasks = relationship("TaskModel", back_populates="session")
    current_leaf_message = relationship(
        "MessageModel",
        primaryjoin=(
            "and_(foreign(SessionModel.current_leaf_message_id) == "
            "remote(MessageModel.message_id), "
            "foreign(SessionModel.session_id) == remote(MessageModel.session_id))"
        ),
        viewonly=True,
        uselist=False,
    )

