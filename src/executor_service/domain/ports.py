"""Ports implemented by infrastructure adapters."""

from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from executor_service.domain.models import Execution, OutboxEvent


class ExecutionRepository(Protocol):
    async def add(self, execution: Execution) -> None: ...

    async def get(self, execution_id: UUID, *, for_update: bool = False) -> Execution | None: ...

    async def get_by_submit_key(self, idempotency_key: str) -> Execution | None: ...

    async def get_by_cancel_key(self, idempotency_key: str) -> Execution | None: ...

    async def get_by_retry_key(self, idempotency_key: str) -> Execution | None: ...

    async def add_retry_receipt(
        self, execution_id: UUID, idempotency_key: str, from_sequence: int
    ) -> None: ...

    async def save(self, execution: Execution) -> None: ...


class OutboxRepository(Protocol):
    async def add(self, event: OutboxEvent) -> None: ...


class UnitOfWork(Protocol):
    @property
    def executions(self) -> ExecutionRepository: ...

    @property
    def outbox(self) -> OutboxRepository: ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class JupyterGateway(Protocol):
    """Adapter boundary for REST/WebSocket-backed notebook execution."""

    async def submit(self, execution: Execution) -> None: ...

    async def cancel(self, execution_id: UUID) -> None: ...
