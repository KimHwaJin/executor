from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import Enum, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import DeleteYN, enum_values
from app.models.model_base import Base, TimestampMixin


class UserModel(TimestampMixin, Base):
    """서비스 사용자."""

    __tablename__ = "users"

    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    user_name: Mapped[str] = mapped_column(String(100), nullable=False)
    delete_yn: Mapped[DeleteYN] = mapped_column(
        Enum(
            DeleteYN,
            name="delete_yn",
            values_callable=enum_values,
        ),
        nullable=False,
        default=DeleteYN.N,
        server_default=DeleteYN.N.value,
    )

    owned_projects = relationship(
        "ProjectModel",
        back_populates="owner",
        foreign_keys="ProjectModel.user_id",
    )
    project_memberships = relationship(
        "ProjectMemberModel",
        back_populates="user",
        foreign_keys="ProjectMemberModel.user_id",
    )
    sessions = relationship(
        "SessionModel",
        back_populates="user",
        foreign_keys="SessionModel.user_id",
    )

