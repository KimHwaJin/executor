"""Read models and pagination for Executor Maintenance Runs."""

from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.application.maintenance_runs import (
    MaintenanceRunCounts,
    MaintenanceRunTargetView,
    MaintenanceRunView,
)
from executor_service.application.pagination import (
    Page,
    decode_time_cursor,
    encode_time_cursor,
)
from executor_service.domain.enums import MaintenanceRunTargetStatus
from executor_service.domain.errors import MaintenanceRunNotFoundError
from executor_service.infrastructure._maintenance_runs.constants import (
    RUN_CURSOR_KIND,
)
from executor_service.infrastructure.db.models import (
    MaintenanceRunORM,
    MaintenanceRunTargetORM,
)


class MaintenanceRunQueries:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        self._session_factory = session_factory

    async def get(self, run_id: UUID) -> MaintenanceRunView:
        async with self._session_factory() as session:
            run = await self.required_run(session, run_id)
            return await self.view(session, run)

    async def list_targets(
        self,
        run_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[MaintenanceRunTargetView]:
        async with self._session_factory() as session:
            await self.required_run(session, run_id)
            statement = select(MaintenanceRunTargetORM).where(
                MaintenanceRunTargetORM.maintenance_run_id == run_id
            )
            if cursor is not None:
                created_at, target_id = decode_time_cursor(
                    cursor, RUN_CURSOR_KIND
                )
                statement = statement.where(
                    or_(
                        MaintenanceRunTargetORM.created_at > created_at,
                        and_(
                            MaintenanceRunTargetORM.created_at == created_at,
                            MaintenanceRunTargetORM.id > target_id,
                        ),
                    )
                )
            rows = list(
                await session.scalars(
                    statement.order_by(
                        MaintenanceRunTargetORM.created_at,
                        MaintenanceRunTargetORM.id,
                    ).limit(limit + 1)
                )
            )
            page_rows = rows[:limit]
            next_cursor = None
            if len(rows) > limit and page_rows:
                last = page_rows[-1]
                next_cursor = encode_time_cursor(
                    RUN_CURSOR_KIND,
                    last.created_at,
                    last.id,
                )
            return Page(
                items=[target_view(row) for row in page_rows],
                next_cursor=next_cursor,
            )

    async def view(
        self, session: AsyncSession, run: MaintenanceRunORM
    ) -> MaintenanceRunView:
        return MaintenanceRunView(
            id=run.id,
            action=run.action,
            status=run.status,
            counts=await self.counts(session, run.id),
            error_message=run.error_message,
            created_by_type=run.created_by_type,
            created_by=run.created_by,
            updated_by_type=run.updated_by_type,
            updated_by=run.updated_by,
            created_at=run.created_at,
            updated_at=run.updated_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
        )

    @staticmethod
    async def required_run(
        session: AsyncSession, run_id: UUID
    ) -> MaintenanceRunORM:
        run = await session.get(MaintenanceRunORM, run_id)
        if run is None:
            raise MaintenanceRunNotFoundError(
                f"Maintenance Run {run_id} was not found."
            )
        return run

    @staticmethod
    async def counts(
        session: AsyncSession, run_id: UUID
    ) -> MaintenanceRunCounts:
        result = await session.execute(
            select(
                MaintenanceRunTargetORM.status,
                func.count(MaintenanceRunTargetORM.id),
            )
            .where(MaintenanceRunTargetORM.maintenance_run_id == run_id)
            .group_by(MaintenanceRunTargetORM.status)
        )
        grouped = dict(list(result.tuples()))
        return MaintenanceRunCounts(
            total=sum(grouped.values()),
            pending=grouped.get(MaintenanceRunTargetStatus.PENDING, 0),
            stop_requested=grouped.get(
                MaintenanceRunTargetStatus.STOP_REQUESTED,
                0,
            ),
            stopped=grouped.get(MaintenanceRunTargetStatus.STOPPED, 0),
            failed=grouped.get(MaintenanceRunTargetStatus.FAILED, 0),
        )


def target_view(
    target: MaintenanceRunTargetORM,
) -> MaintenanceRunTargetView:
    return MaintenanceRunTargetView(
        id=target.id,
        maintenance_run_id=target.maintenance_run_id,
        execution_id=target.execution_id,
        selected_execution_status=target.selected_execution_status,
        status=target.status,
        error_message=target.error_message,
        stop_requested_at=target.stop_requested_at,
        completed_at=target.completed_at,
        created_by_type=target.created_by_type,
        created_by=target.created_by,
        updated_by_type=target.updated_by_type,
        updated_by=target.updated_by,
        created_at=target.created_at,
        updated_at=target.updated_at,
    )
