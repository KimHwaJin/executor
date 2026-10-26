from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.model_base import Base, TimestampMixin


class JupyterServerModel(TimestampMixin, Base):
    """E10-T04 연결 정보 Registry. Kernel/Pool lease는 이 테이블의 책임이 아닙니다."""

    __tablename__ = "jupyter_servers"

    jupyter_server_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(1000), nullable=False)
    # Token 평문은 절대 저장하거나 응답하지 않습니다.
    token_ciphertext: Mapped[str | None] = mapped_column(Text)
    health_status: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown", server_default="unknown", index=True)
    last_http_status: Mapped[int | None] = mapped_column(Integer)
    last_latency_ms: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(String(500))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="SET NULL"), index=True
    )

    __table_args__ = (
        CheckConstraint("health_status IN ('unknown', 'healthy', 'unhealthy')", name="ck_jupyter_servers_health_status"),
        Index("uq_jupyter_servers_active_name", "name", unique=True, postgresql_where=text("deleted_at IS NULL")),
        Index("uq_jupyter_servers_active_endpoint", "endpoint", unique=True, postgresql_where=text("deleted_at IS NULL")),
    )

