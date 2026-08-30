"""SQLAlchemy reads for Runtime Targets and Runtime Pools."""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.application.pagination import (
    Page,
    decode_time_cursor,
    encode_time_cursor,
)
from executor_service.application.runtime_targets import (
    RuntimePoolView,
    RuntimeTargetView,
)
from executor_service.config import Settings
from executor_service.domain.enums import (
    RuntimePool,
    RuntimeTargetStatus,
    RuntimeType,
)
from executor_service.infrastructure._runtime_registry.mappers import (
    pool_summary,
    runtime_target_view,
)
from executor_service.infrastructure._runtime_registry.targets import (
    required_target,
)
from executor_service.infrastructure.db.models import RuntimeTargetORM


class RuntimeTargetQueries:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings

    async def list(
        self,
        pool: RuntimePool | None = None,
        *,
        runtime_type: RuntimeType | None = None,
        status: RuntimeTargetStatus | None = None,
        enabled: bool | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[RuntimeTargetView]:
        async with self._session_factory() as session:
            statement = select(RuntimeTargetORM)
            if pool is not None:
                statement = statement.where(RuntimeTargetORM.pool == pool)
            if runtime_type is not None:
                statement = statement.where(
                    RuntimeTargetORM.runtime_type == runtime_type
                )
            if status is not None:
                statement = statement.where(RuntimeTargetORM.status == status)
            if enabled is not None:
                statement = statement.where(
                    RuntimeTargetORM.enabled.is_(enabled)
                )
            if cursor is not None:
                created_at, item_id = decode_time_cursor(
                    cursor, "runtime_targets"
                )
                statement = statement.where(
                    or_(
                        RuntimeTargetORM.created_at > created_at,
                        and_(
                            RuntimeTargetORM.created_at == created_at,
                            RuntimeTargetORM.id > item_id,
                        ),
                    )
                )
            statement = statement.order_by(
                RuntimeTargetORM.created_at, RuntimeTargetORM.id
            ).limit(limit + 1)
            targets = list(await session.scalars(statement))
            page_targets = targets[:limit]
            views = [
                await self.view(session, target) for target in page_targets
            ]
        next_cursor = (
            encode_time_cursor(
                "runtime_targets",
                page_targets[-1].created_at,
                page_targets[-1].id,
            )
            if len(targets) > limit and page_targets
            else None
        )
        return Page(items=views, next_cursor=next_cursor)

    async def pool_summaries(self) -> Sequence[RuntimePoolView]:
        async with self._session_factory() as session:
            targets = list(
                await session.scalars(
                    select(RuntimeTargetORM).order_by(
                        RuntimeTargetORM.created_at
                    )
                )
            )
            views = [await self.view(session, target) for target in targets]

        summaries: list[RuntimePoolView] = []
        for runtime_type in RuntimeType:
            for pool in RuntimePool:
                pool_views = [
                    view
                    for view in views
                    if view.runtime_type == runtime_type and view.pool == pool
                ]
                summaries.append(pool_summary(runtime_type, pool, pool_views))
        return summaries

    async def get(self, target_id: UUID) -> RuntimeTargetView:
        async with self._session_factory() as session:
            target = await required_target(session, target_id)
            return await self.view(session, target)

    async def view(
        self,
        session: AsyncSession,
        target: RuntimeTargetORM,
    ) -> RuntimeTargetView:
        return await runtime_target_view(session, target, self._settings)
