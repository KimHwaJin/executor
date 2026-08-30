"""Execution Artifact metadata and content routes."""

from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Header, Response, status
from fastapi.responses import StreamingResponse

from executor_service.container import ApplicationContainer
from executor_service.interfaces.contracts import (
    ExecutionArtifactMaterializeRequest,
    ExecutionArtifactPageResponse,
    ExecutionArtifactResponse,
)
from executor_service.interfaces.http._executions.common import (
    DOMAIN_ERROR_RESPONSES,
    ArtifactLimit,
    Cursor,
    execution_router,
)


def build_artifact_router(container: ApplicationContainer) -> APIRouter:
    router = execution_router()
    execution_queries = container.execution_queries

    @router.get(
        "/executions/{execution_id}/artifacts",
        response_model=ExecutionArtifactPageResponse,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="List execution Artifacts",
    )
    async def list_execution_artifacts(
        execution_id: UUID,
        cursor: Cursor = None,
        limit: ArtifactLimit = 100,
    ) -> ExecutionArtifactPageResponse:
        page = await execution_queries.artifacts(
            execution_id, cursor=cursor, limit=limit
        )
        return ExecutionArtifactPageResponse.from_page(page)

    @router.post(
        "/executions/{execution_id}/artifacts",
        response_model=ExecutionArtifactResponse,
        status_code=status.HTTP_201_CREATED,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="Materialize Agent-authored text as a Runtime-owned Artifact",
    )
    async def materialize_execution_artifact(
        execution_id: UUID,
        request: ExecutionArtifactMaterializeRequest,
        response: Response,
    ) -> ExecutionArtifactResponse:
        artifact_id = await container.materialized_artifacts.materialize(
            request.to_command(execution_id)
        )
        response.headers["Location"] = f"/api/v1/artifacts/{artifact_id}"
        view = await execution_queries.artifact(artifact_id)
        return ExecutionArtifactResponse.from_view(view)

    @router.get(
        "/artifacts/{artifact_id}",
        response_model=ExecutionArtifactResponse,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="Get one execution Artifact and lineage references",
    )
    async def get_execution_artifact(
        artifact_id: UUID,
    ) -> ExecutionArtifactResponse:
        view = await execution_queries.artifact(artifact_id)
        return ExecutionArtifactResponse.from_view(view)

    @router.get(
        "/artifacts/{artifact_id}/content",
        responses={
            **DOMAIN_ERROR_RESPONSES,
            206: {"description": "Requested Artifact byte range"},
            416: {"description": "Artifact byte range is not satisfiable"},
        },
        summary="Stream registered Artifact content",
    )
    async def download_execution_artifact(
        artifact_id: UUID,
        range_header: Annotated[str | None, Header(alias="Range")] = None,
    ) -> StreamingResponse:
        content = await container.artifact_content.open(
            artifact_id, range_header
        )
        byte_range = content.byte_range
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(byte_range.length),
            "Content-Disposition": (
                "attachment; filename*=UTF-8''" + quote(content.name, safe="")
            ),
            "ETag": f'"{content.checksum_sha256}"',
            "X-Checksum-SHA256": content.checksum_sha256,
        }
        if byte_range.partial:
            headers["Content-Range"] = (
                f"bytes {byte_range.start}-{byte_range.end}/{byte_range.size}"
            )
        return StreamingResponse(
            content.body,
            status_code=(
                status.HTTP_206_PARTIAL_CONTENT
                if byte_range.partial
                else status.HTTP_200_OK
            ),
            media_type=content.media_type,
            headers=headers,
        )

    return router
