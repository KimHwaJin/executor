"""Persistent Runtime Target registry, encrypted credentials, and health monitoring."""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.application.pagination import Page
from executor_service.application.runtime_targets import (
    DisableRuntimeTargetCommand,
    PurgeRuntimeTargetCommand,
    RuntimePoolView,
    RuntimeTargetPurgeView,
    RuntimeTargetView,
    SetRuntimeTargetStateCommand,
    UpsertRuntimeTargetCommand,
)
from executor_service.config import Settings
from executor_service.domain.enums import (
    ActorType,
    RuntimePool,
    RuntimeTargetStatus,
    RuntimeType,
)
from executor_service.infrastructure._runtime_registry import (
    RuntimeCommandReceipts,
    RuntimeCredentialCipher,
    RuntimeHealthMonitor,
    RuntimeTargetCommands,
    RuntimeTargetProber,
    RuntimeTargetQueries,
)


class RuntimeTargetRegistry:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self._credentials = RuntimeCredentialCipher(
            settings.runtime_credential_encryption_key
        )
        receipts = RuntimeCommandReceipts()
        self._queries = RuntimeTargetQueries(session_factory, settings)
        self._prober = RuntimeTargetProber(
            session_factory,
            settings,
            self._credentials,
        )
        self._monitor = RuntimeHealthMonitor(
            session_factory,
            settings.runtime_health_poll_interval_seconds,
            self._prober,
        )
        self._commands = RuntimeTargetCommands(
            session_factory,
            settings,
            self._credentials,
            receipts,
            self._queries,
            self._prober,
        )

    async def start(self) -> None:
        await self._monitor.start()

    async def stop(self) -> None:
        await self._monitor.stop()

    async def upsert(
        self, command: UpsertRuntimeTargetCommand
    ) -> RuntimeTargetView:
        return await self._commands.upsert(command)

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
        return await self._queries.list(
            pool,
            runtime_type=runtime_type,
            status=status,
            enabled=enabled,
            cursor=cursor,
            limit=limit,
        )

    async def pool_summaries(self) -> Sequence[RuntimePoolView]:
        return await self._queries.pool_summaries()

    async def get(self, target_id: UUID) -> RuntimeTargetView:
        return await self._queries.get(target_id)

    async def probe(
        self,
        target_id: UUID,
        *,
        actor_type: ActorType | None = None,
        actor_id: str | None = None,
    ) -> RuntimeTargetView:
        return await self._prober.probe(
            target_id,
            actor_type=actor_type,
            actor_id=actor_id,
        )

    async def disable(
        self, command: DisableRuntimeTargetCommand
    ) -> RuntimeTargetView:
        return await self._commands.disable(command)

    async def set_state(
        self, command: SetRuntimeTargetStateCommand
    ) -> RuntimeTargetView:
        return await self._commands.set_state(command)

    async def purge(
        self, command: PurgeRuntimeTargetCommand
    ) -> RuntimeTargetPurgeView:
        return await self._commands.purge(command)

    def resolve_credential(
        self, credential_ref: str, credential_ciphertext: str | None
    ) -> str:
        return self._credentials.resolve(credential_ref, credential_ciphertext)
