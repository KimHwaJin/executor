"""Execution Artifact command and response contracts."""

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from executor_service.application.commands import MaterializeArtifactCommand
from executor_service.application.execution_queries import (
    ExecutionArtifactView,
)
from executor_service.application.pagination import Page
from executor_service.domain.enums import (
    ArtifactStatus,
    ArtifactStorageType,
    ArtifactType,
    CodeSourceType,
)
from executor_service.interfaces._contracts.common import (
    ActorInput,
    AuditFields,
    ContractModel,
    PageResponse,
)


class InlineArtifactSource(ContractModel):
    type: Literal[CodeSourceType.INLINE]
    content: str = Field(min_length=1)


class PathArtifactSource(ContractModel):
    type: Literal[CodeSourceType.PATH]
    path: str = Field(min_length=1, max_length=4096)
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")


ArtifactSource = Annotated[
    InlineArtifactSource | PathArtifactSource,
    Field(discriminator="type"),
]

MaterializableArtifactType = Literal[
    ArtifactType.NOTEBOOK,
    ArtifactType.REPORT,
    ArtifactType.PLOT,
    ArtifactType.METRIC,
    ArtifactType.LOG,
    ArtifactType.OTHER,
]


class ExecutionArtifactMaterializeRequest(ContractModel):
    idempotency_key: str = Field(min_length=1, max_length=255)
    type: MaterializableArtifactType
    source: ArtifactSource
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4000)
    media_type: str | None = Field(default=None, min_length=1, max_length=255)
    append_to_notebook: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    actor: ActorInput

    @model_validator(mode="after")
    def validate_notebook_append(
        self,
    ) -> "ExecutionArtifactMaterializeRequest":
        if self.append_to_notebook and self.type != ArtifactType.REPORT:
            raise ValueError(
                "append_to_notebook is supported only for REPORT Artifacts."
            )
        return self

    def to_command(self, execution_id: UUID) -> MaterializeArtifactCommand:
        return MaterializeArtifactCommand(
            execution_id=execution_id,
            idempotency_key=self.idempotency_key,
            artifact_type=self.type,
            source_type=self.source.type,
            source_content=(
                self.source.content
                if isinstance(self.source, InlineArtifactSource)
                else None
            ),
            source_path=(
                self.source.path
                if isinstance(self.source, PathArtifactSource)
                else None
            ),
            source_sha256=(
                self.source.sha256
                if isinstance(self.source, PathArtifactSource)
                else None
            ),
            name=self.name,
            description=self.description,
            media_type=self.media_type,
            append_to_notebook=self.append_to_notebook,
            metadata=self.metadata,
            actor_type=self.actor.type,
            actor_id=self.actor.id,
        )


class ArtifactProducer(ContractModel):
    execution_id: UUID
    execution_attempt_id: UUID | None
    execution_step_id: UUID | None
    execution_step_attempt_id: UUID | None


class ArtifactLineage(ContractModel):
    parent_artifact_id: UUID | None
    external_parent_asset_id: str | None


class ArtifactStorage(ContractModel):
    type: ArtifactStorageType
    uri: str
    relative_path: str | None
    media_type: str | None
    size_bytes: int | None
    checksum_sha256: str | None


class ExecutionArtifactResponse(AuditFields):
    artifact_id: UUID
    name: str
    description: str | None
    type: ArtifactType
    status: ArtifactStatus
    produced_by: ArtifactProducer
    lineage: ArtifactLineage
    storage: ArtifactStorage
    metadata: dict[str, Any]

    @classmethod
    def from_view(
        cls,
        view: ExecutionArtifactView,
    ) -> "ExecutionArtifactResponse":
        return cls(
            artifact_id=view.id,
            name=view.name,
            description=view.description,
            type=view.artifact_type,
            status=view.status,
            produced_by=ArtifactProducer(
                execution_id=view.execution_id,
                execution_attempt_id=view.execution_attempt_id,
                execution_step_id=view.execution_step_id,
                execution_step_attempt_id=view.execution_step_attempt_id,
            ),
            lineage=ArtifactLineage(
                parent_artifact_id=view.parent_artifact_id,
                external_parent_asset_id=view.external_parent_asset_id,
            ),
            storage=ArtifactStorage(
                type=view.storage_type,
                uri=view.uri,
                relative_path=view.relative_path,
                media_type=view.media_type,
                size_bytes=view.size_bytes,
                checksum_sha256=view.checksum_sha256,
            ),
            metadata=view.metadata,
            created_by_type=view.created_by_type,
            created_by=view.created_by,
            updated_by_type=view.updated_by_type,
            updated_by=view.updated_by,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )


class ArtifactStorageSummary(ContractModel):
    type: ArtifactStorageType
    media_type: str | None
    size_bytes: int | None


class ExecutionArtifactSummaryResponse(AuditFields):
    artifact_id: UUID
    name: str
    type: ArtifactType
    status: ArtifactStatus
    produced_by: ArtifactProducer
    storage: ArtifactStorageSummary

    @classmethod
    def from_view(
        cls,
        view: ExecutionArtifactView,
    ) -> "ExecutionArtifactSummaryResponse":
        return cls(
            artifact_id=view.id,
            name=view.name,
            type=view.artifact_type,
            status=view.status,
            produced_by=ArtifactProducer(
                execution_id=view.execution_id,
                execution_attempt_id=view.execution_attempt_id,
                execution_step_id=view.execution_step_id,
                execution_step_attempt_id=view.execution_step_attempt_id,
            ),
            storage=ArtifactStorageSummary(
                type=view.storage_type,
                media_type=view.media_type,
                size_bytes=view.size_bytes,
            ),
            created_by_type=view.created_by_type,
            created_by=view.created_by,
            updated_by_type=view.updated_by_type,
            updated_by=view.updated_by,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )


class ExecutionArtifactPageResponse(PageResponse):
    items: list[ExecutionArtifactSummaryResponse]

    @classmethod
    def from_page(
        cls,
        page: Page[ExecutionArtifactView],
    ) -> "ExecutionArtifactPageResponse":
        return cls(
            items=[
                ExecutionArtifactSummaryResponse.from_view(item)
                for item in page.items
            ],
            next_cursor=page.next_cursor,
            has_more=page.next_cursor is not None,
        )
