from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Enum,
    ForeignKey,
    Identity,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import (
    DeleteYN,
    MessageStatus,
    MessageType,
    enum_values,
)
from app.models.model_base import Base, TimestampMixin


class MessageModel(TimestampMixin, Base):
    """Session 내부 사용자/Assistant/Tool 메시지."""

    __tablename__ = "messages"

    message_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    session_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sessions.session_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    message_type: Mapped[MessageType] = mapped_column(
        Enum(
            MessageType,
            name="message_type",
            values_callable=enum_values,
        ),
        nullable=False,
    )
    content: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default="[]",
    )
    content_text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="",
        server_default="",
    )
    message_status: Mapped[MessageStatus] = mapped_column(
        Enum(
            MessageStatus,
            name="message_status",
            values_callable=enum_values,
        ),
        nullable=False,
        default=MessageStatus.COMPLETED,
        server_default=MessageStatus.COMPLETED.value,
    )

    client_request_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
    )
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    sequence_no: Mapped[int] = mapped_column(
        BigInteger,
        Identity(),
        nullable=False,
        unique=True,
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
        UniqueConstraint(
            "message_id",
            "session_id",
            name="uq_messages_message_session",
        ),
        Index(
            "uq_messages_client_request",
            "session_id",
            "client_request_id",
            unique=True,
            postgresql_where=client_request_id.is_not(None),
        ),
    )

    session = relationship(
        "SessionModel",
        back_populates="messages",
        foreign_keys=[session_id],
    )
    trigger_llm_runs = relationship(
        "LLMRunModel",
        back_populates="trigger_message",
        foreign_keys="LLMRunModel.trigger_message_id",
    )
    assistant_llm_runs = relationship(
        "LLMRunModel",
        back_populates="assistant_message",
        foreign_keys="LLMRunModel.assistant_message_id",
    )

