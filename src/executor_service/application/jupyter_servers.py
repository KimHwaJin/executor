"""Application contracts for managing the Jupyter server fleet."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from executor_service.application.pagination import Page
from executor_service.domain.enums import ActorType, JupyterPool, JupyterServerStatus


@dataclass(frozen=True, slots=True)
class UpsertJupyterServerCommand:
    idempotency_key: str
    name: str
    endpoint: str
    token: str | None
    pool: JupyterPool
    actor_type: ActorType | None = None
    actor_id: str | None = None
    max_concurrent_executions: int | None = None


@dataclass(frozen=True, slots=True)
class RemoveJupyterServerCommand:
    idempotency_key: str
    server_id: UUID
    actor_type: ActorType | None = None
    actor_id: str | None = None


@dataclass(frozen=True, slots=True)
class SetJupyterServerStateCommand:
    idempotency_key: str
    server_id: UUID
    desired_state: JupyterServerStatus
    actor_type: ActorType | None = None
    actor_id: str | None = None


@dataclass(frozen=True, slots=True)
class JupyterServerView:
    id: UUID
    name: str
    endpoint: str
    pool: JupyterPool
    status: JupyterServerStatus
    enabled: bool
    max_concurrent_executions: int
    supported_kernels: tuple[str, ...]
    active_execution_count: int
    active_kernel_count: int | None
    last_health_check_at: datetime | None
    last_health_error: str | None
    created_by_type: ActorType | None
    created_by: str | None
    updated_by_type: ActorType | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def accepting_new_executions(self) -> bool:
        return self.enabled and self.status == JupyterServerStatus.ACTIVE

    @property
    def drain_complete(self) -> bool:
        return self.status == JupyterServerStatus.DRAINING and self.active_execution_count == 0


class JupyterServerManager(Protocol):
    async def upsert(self, command: UpsertJupyterServerCommand) -> JupyterServerView: ...

    async def list(
        self,
        pool: JupyterPool | None = None,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[JupyterServerView]: ...

    async def get(self, server_id: UUID) -> JupyterServerView: ...

    async def probe(
        self,
        server_id: UUID,
        *,
        actor_type: ActorType | None = None,
        actor_id: str | None = None,
    ) -> JupyterServerView: ...

    async def remove(self, command: RemoveJupyterServerCommand) -> JupyterServerView: ...

    async def set_state(self, command: SetJupyterServerStateCommand) -> JupyterServerView: ...
