"""SQLAlchemy reads for Execution Artifacts."""

from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.application.execution_queries import (
    ExecutionArtifactView,
)
from executor_service.application.pagination import (
    Page,
    decode_time_cursor,
    encode_time_cursor,
)
from executor_service.domain.errors import ExecutionArtifactNotFoundError
from executor_service.infrastructure._execution_queries.guards import (
    require_execution,
)
from executor_service.infrastructure._execution_queries.mappers import (
    artifact_view,
)
from executor_service.infrastructure.db.models import ExecutionArtifactORM


class SQLAlchemyArtifactReader:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        self._session_factory = session_factory

    async def artifacts(
        self,
        execution_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[ExecutionArtifactView]:
        async with self._session_factory() as session:
            await require_execution(session, execution_id)
            statement = select(ExecutionArtifactORM).where(
                ExecutionArtifactORM.execution_id == execution_id
            )
            if cursor is not None:
                created_at, item_id = decode_time_cursor(
                    cursor, "execution_artifacts"
                )
                statement = statement.where(
                    or_(
                        ExecutionArtifactORM.created_at > created_at,
                        and_(
                            ExecutionArtifactORM.created_at == created_at,
                            ExecutionArtifactORM.id > item_id,
                        ),
                    )
                )
            rows = list(
                await session.scalars(
                    statement.order_by(
                        ExecutionArtifactORM.created_at,
                        ExecutionArtifactORM.id,
                    ).limit(limit + 1)
                )
            )
        page_rows = rows[:limit]
        next_cursor = (
            encode_time_cursor(
                "execution_artifacts",
                page_rows[-1].created_at,
                page_rows[-1].id,
            )
            if len(rows) > limit and page_rows
            else None
        )
        return Page(
            items=[artifact_view(row) for row in page_rows],
            next_cursor=next_cursor,
        )

    async def artifact(self, artifact_id: UUID) -> ExecutionArtifactView:
        async with self._session_factory() as session:
            row = await session.get(ExecutionArtifactORM, artifact_id)
        if row is None:
            raise ExecutionArtifactNotFoundError(
                f"Execution Artifact {artifact_id} was not found."
            )
        return artifact_view(row)
