"""Public facade for durable Executor Maintenance Runs."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.application.maintenance_runs import (
    CreateMaintenanceRunCommand,
    MaintenanceRunTargetView,
    MaintenanceRunView,
)
from executor_service.application.pagination import Page
from executor_service.application.services import ExecutionService
from executor_service.infrastructure._maintenance_runs import (
    MaintenanceRunCommands,
    MaintenanceRunQueries,
    MaintenanceRunReconciler,
)


class MaintenanceRunService:
    """Stable facade that delegates commands, queries, and reconciliation."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        execution_service: ExecutionService,
        *,
        lease_seconds: int,
    ) -> None:
        queries = MaintenanceRunQueries(session_factory)
        self._commands = MaintenanceRunCommands(session_factory, queries)
        self._queries = queries
        self._reconciler = MaintenanceRunReconciler(
            session_factory,
            execution_service,
            queries,
            lease_seconds=lease_seconds,
        )

    async def create(
        self, command: CreateMaintenanceRunCommand
    ) -> MaintenanceRunView:
        return await self._commands.create(command)

    async def get(self, run_id: UUID) -> MaintenanceRunView:
        return await self._queries.get(run_id)

    async def list_targets(
        self,
        run_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[MaintenanceRunTargetView]:
        return await self._queries.list_targets(
            run_id,
            cursor=cursor,
            limit=limit,
        )

    async def reconcile_once(self, owner: str) -> bool:
        return await self._reconciler.reconcile_once(owner)
