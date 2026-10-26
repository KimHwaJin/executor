"""Agent DB metadata, deliberately separate from chatapp's ORM Base.

The chatapp also owns a workflow_embeddings table with an unrelated schema.
Do not register these tables in app.models.model_base.Base.
"""
from datetime import datetime
import json
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, Text, CHAR, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import UserDefinedType


class AgentWorkflowBase(DeclarativeBase):
    pass


class Vector(UserDefinedType):
    """Dimension-free pgvector SQL type; service binds via explicit SQL casts."""
    cache_ok = True

    def get_col_spec(self, **kw):
        return "VECTOR"

    def bind_processor(self, dialect):
        return lambda value: None if value is None else json.dumps(value, allow_nan=False)

    def result_processor(self, dialect, coltype):
        return lambda value: json.loads(value) if isinstance(value, str) else value


class WorkflowCatalog(AgentWorkflowBase):
    __tablename__ = "workflow_catalog"
    workflow_id: Mapped[UUID] = mapped_column(PG_UUID, primary_key=True, default=uuid4)
    current_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    recommend_yn: Mapped[str] = mapped_column(CHAR(1), nullable=False, server_default="N")
    created_by: Mapped[UUID] = mapped_column(PG_UUID, nullable=False)
    updated_by: Mapped[UUID] = mapped_column(PG_UUID, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_by: Mapped[UUID | None] = mapped_column(PG_UUID)
    __table_args__ = (
        CheckConstraint("current_revision >= 1"),
        CheckConstraint("recommend_yn IN ('Y', 'N')"),
        CheckConstraint("(deleted_at IS NULL) = (deleted_by IS NULL)"),
        ForeignKeyConstraint(
            ["workflow_id", "current_revision"], ["workflow_version.workflow_id", "workflow_version.revision"],
            name="workflow_catalog_current_version_fkey", ondelete="NO ACTION", deferrable=True, initially="DEFERRED", use_alter=True,
        ),
        Index("workflow_catalog_recommendable", "workflow_id", postgresql_where=text("recommend_yn = 'Y' AND deleted_at IS NULL")),
    )


class WorkflowVersion(AgentWorkflowBase):
    __tablename__ = "workflow_version"
    version_id: Mapped[UUID] = mapped_column(PG_UUID, primary_key=True, default=uuid4)
    workflow_id: Mapped[UUID] = mapped_column(PG_UUID, ForeignKey("workflow_catalog.workflow_id", ondelete="RESTRICT"), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    workflow: Mapped[dict] = mapped_column(JSONB, nullable=False)
    registration_type: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[UUID] = mapped_column(PG_UUID, nullable=False)
    project_id: Mapped[UUID | None] = mapped_column(PG_UUID)
    session_id: Mapped[UUID | None] = mapped_column(PG_UUID)
    task_id: Mapped[UUID | None] = mapped_column(PG_UUID)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint("workflow_id", "revision"),
        CheckConstraint("revision >= 1"),
        CheckConstraint("registration_type IN ('USER', 'AGENT')"),
        CheckConstraint("jsonb_typeof(workflow) = 'object' AND workflow @> '{\"workflow_version\":\"1.0\"}'::jsonb AND jsonb_typeof(workflow->'workflow') IS NOT DISTINCT FROM 'object'"),
    )


class WorkflowEmbedding(AgentWorkflowBase):
    __tablename__ = "workflow_embeddings"
    embedding_id: Mapped[UUID] = mapped_column(PG_UUID, primary_key=True, default=uuid4)
    version_id: Mapped[UUID] = mapped_column(PG_UUID, ForeignKey("workflow_version.version_id", ondelete="RESTRICT"), nullable=False)
    search_text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_model: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(), nullable=False)
    embedding_dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    text_recipe_version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint("version_id", "embedding_model"),
        CheckConstraint("length(btrim(search_text)) > 0"),
        CheckConstraint("length(btrim(embedding_model)) > 0"),
        CheckConstraint("length(btrim(text_recipe_version)) > 0"),
        CheckConstraint("embedding_dimension > 0 AND embedding_dimension = vector_dims(embedding)"),
        Index("workflow_embeddings_model_dimension", "embedding_model", "embedding_dimension"),
    )


class WorkflowCatalogRequest(AgentWorkflowBase):
    __tablename__ = "workflow_catalog_requests"
    requested_by: Mapped[UUID] = mapped_column(PG_UUID, primary_key=True)
    request_key: Mapped[str] = mapped_column(Text, primary_key=True)
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    __table_args__ = (
        CheckConstraint("length(request_key) BETWEEN 1 AND 255 AND request_key = btrim(request_key)",
                        name="workflow_catalog_requests_key_check"),
        CheckConstraint("request_hash ~ '^[0-9a-f]{64}$'", name="workflow_catalog_requests_hash_check"),
        CheckConstraint("jsonb_typeof(result) = 'object'", name="workflow_catalog_requests_result_check"),
    )


class WorkflowExecution(AgentWorkflowBase):
    __tablename__ = "workflow_executions"
    execution_id: Mapped[UUID] = mapped_column(PG_UUID, primary_key=True)
    version_id: Mapped[UUID | None] = mapped_column(PG_UUID, ForeignKey("workflow_version.version_id", ondelete="RESTRICT"))
    workflow_id: Mapped[str] = mapped_column(Text, nullable=False)
    workflow_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    session_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    original_workflow: Mapped[dict] = mapped_column(JSONB, nullable=False)
    execution_inputs: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    final_workflow: Mapped[dict | None] = mapped_column(JSONB)
    execution_status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'running'"))
    successful: Mapped[bool | None] = mapped_column()
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    __table_args__ = (
        CheckConstraint("jsonb_typeof(execution_inputs) = 'array'"),
        Index("workflow_executions_task_lookup", "session_id", "task_id"),
        Index("workflow_executions_version_lookup", "version_id"),
    )
