"""Execution artifact SQLAlchemy ORM model."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from executor_service.domain.enums import (
    ActorType,
    ArtifactStatus,
    ArtifactStorageType,
    ArtifactType,
)
from executor_service.domain.models import utc_now
from executor_service.infrastructure.db._models.common import (
    audit_actor_constraints,
    enum_type,
)
from executor_service.infrastructure.db.base import Base


class ExecutionArtifactORM(Base):
    """Artifact evidence attached at Execution, Attempt, or Step scope."""

    __tablename__ = "execution_artifacts"
    __table_args__ = (
        *audit_actor_constraints(),
        CheckConstraint(
            "artifact_type IN ('DATASET', 'NOTEBOOK', 'REPORT', 'PLOT', 'MODEL', "
            "'METRIC', 'LOG', 'OTHER')",
            name="valid_artifact_type",
        ),
        CheckConstraint(
            "storage_type IN ('PV', 'S3')", name="valid_artifact_storage_type"
        ),
        CheckConstraint(
            "status IN ('AVAILABLE', 'INCOMPLETE', 'DELETED')",
            name="valid_artifact_status",
        ),
        CheckConstraint(
            "size_bytes IS NULL OR size_bytes >= 0", name="non_negative_size"
        ),
        Index(
            "ix_execution_artifacts_execution_created",
            "execution_id",
            "created_at",
            "id",
        ),
        Index(
            "ix_execution_artifacts_step", "execution_step_id", "created_at"
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    execution_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("executions.id", ondelete="CASCADE"),
        nullable=False,
    )
    execution_attempt_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("execution_attempts.id", ondelete="CASCADE"),
        nullable=True,
    )
    execution_step_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("execution_steps.id", ondelete="SET NULL"),
    )
    execution_step_attempt_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("execution_step_attempts.id", ondelete="SET NULL"),
    )
    parent_artifact_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("execution_artifacts.id", ondelete="SET NULL"),
    )
    external_parent_asset_id: Mapped[str | None] = mapped_column(String(255))
    artifact_type: Mapped[ArtifactType] = mapped_column(
        enum_type(ArtifactType, "artifact_type"), nullable=False
    )
    storage_type: Mapped[ArtifactStorageType] = mapped_column(
        enum_type(ArtifactStorageType, "artifact_storage_type"), nullable=False
    )
    status: Mapped[ArtifactStatus] = mapped_column(
        enum_type(ArtifactStatus, "artifact_status"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    uri: Mapped[str] = mapped_column(Text, nullable=False)
    relative_path: Mapped[str | None] = mapped_column(Text)
    media_type: Mapped[str | None] = mapped_column(String(255))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64))
    artifact_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict
    )
    identity_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    created_by_type: Mapped[ActorType | None] = mapped_column(
        enum_type(ActorType, "actor_type"), nullable=True
    )
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_by_type: Mapped[ActorType | None] = mapped_column(
        enum_type(ActorType, "actor_type"), nullable=True
    )
    updated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )
