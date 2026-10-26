"""HTTP contracts for the versioned workflow catalog (Workflow 1.0)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from app.schemas.workflows.workflow_standard import StandardWorkflow

RegistrationType = Literal["USER", "AGENT"]
RecommendYN = Literal["Y", "N"]
ExecutionMode = Literal["static", "adaptive"]
Revision = Annotated[int, Field(ge=1, strict=True)]
Similarity = Annotated[float, Field(ge=-1, le=1, allow_inf_nan=False, strict=True)]


class CatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_serializer("created_at", "updated_at", check_fields=False)
    def serialize_timestamp(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class VersionRequest(CatalogModel):
    workflow: StandardWorkflow
    registration_type: RegistrationType
    project_id: UUID | None = None
    session_id: UUID | None = None
    task_id: UUID | None = None


class RegisterRequest(VersionRequest):
    created_by: UUID


class UpdateRequest(VersionRequest):
    expected_revision: Revision
    updated_by: UUID


class RecommendationRequest(CatalogModel):
    recommend_yn: RecommendYN
    updated_by: UUID


class SearchRequest(CatalogModel):
    query: str = Field(min_length=1, pattern=r"\S")
    limit: int = Field(default=3, ge=1, le=20, strict=True)
    threshold: Similarity | None = None
    execution_mode: ExecutionMode | None = None


class WriteResponse(CatalogModel):
    workflow_id: UUID
    version_id: UUID
    revision: Revision
    registration_type: RegistrationType
    recommend_yn: RecommendYN


class RecommendationResponse(CatalogModel):
    workflow_id: UUID
    current_revision: Revision
    recommend_yn: RecommendYN


class CatalogMetadata(RecommendationResponse):
    created_by: UUID
    updated_by: UUID
    created_at: datetime
    updated_at: datetime


class ListItem(CatalogMetadata):
    version_id: UUID
    name: str
    user_request: str
    execution_mode: ExecutionMode
    allow_immediate_execution: bool
    registration_type: RegistrationType


class ListResponse(CatalogModel):
    items: list[ListItem] = Field(max_length=100)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    total_count: int = Field(ge=0)


class VersionDetail(CatalogModel):
    version_id: UUID
    revision: Revision
    workflow: StandardWorkflow
    registration_type: RegistrationType
    created_by: UUID
    project_id: UUID | None
    session_id: UUID | None
    task_id: UUID | None
    created_at: datetime
    search_text: str | None


class DetailResponse(CatalogMetadata):
    version: VersionDetail


class SearchItem(CatalogModel):
    workflow_id: UUID
    version_id: UUID
    revision: Revision
    similarity: Similarity
    search_text: str
    workflow: StandardWorkflow


class SearchResponse(CatalogModel):
    items: list[SearchItem] = Field(max_length=20)
    returned_count: int = Field(ge=0, le=20)
    limit: int = Field(ge=1, le=20)
    threshold: Similarity | None
    embedding_model: str


class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="allow")
    field: str | None
    reason: str


class CatalogError(CatalogModel):
    code: Literal[
        "REQUEST_VALIDATION_ERROR", "WORKFLOW_VALIDATION_ERROR",
        "WORKFLOW_NOT_FOUND", "WORKFLOW_VERSION_NOT_FOUND",
        "WORKFLOW_REVISION_CONFLICT", "IDEMPOTENCY_KEY_CONFLICT",
        "EMBEDDING_SERVICE_ERROR", "EMBEDDING_TIMEOUT", "INTERNAL_ERROR",
    ]
    message: str
    details: list[ErrorDetail]


class ErrorResponse(CatalogModel):
    error: CatalogError
