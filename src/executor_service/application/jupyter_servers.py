"""Application contracts for managing the Jupyter server fleet."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from executor_service.domain.enums import JupyterPool, JupyterServerStatus


@dataclass(frozen=True, slots=True)
class UpsertJupyterServerCommand:
    idempotency_key: str
    name: str
    endpoint: str
    token: str | None
    pool: JupyterPool
    max_concurrent_executions: int | None = None


@dataclass(frozen=True, slots=True)
class RemoveJupyterServerCommand:
    idempotency_key: str
    server_id: UUID


@dataclass(frozen=True, slots=True)
class SetJupyterServerStateCommand:
    idempotency_key: str
    server_id: UUID
    desired_state: JupyterServerStatus


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

    async def list(self, pool: JupyterPool | None = None) -> list[JupyterServerView]: ...

    async def get(self, server_id: UUID) -> JupyterServerView: ...

    async def probe(self, server_id: UUID) -> JupyterServerView: ...

    async def remove(self, command: RemoveJupyterServerCommand) -> JupyterServerView: ...

    async def set_state(
        self, command: SetJupyterServerStateCommand
    ) -> JupyterServerView: ...
