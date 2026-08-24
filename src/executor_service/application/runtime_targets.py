"""Application contracts for managing execution Runtime Targets."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from executor_service.application.pagination import Page
from executor_service.domain.enums import (
    ActorType,
    RuntimePool,
    RuntimeTargetStatus,
    RuntimeType,
)


@dataclass(frozen=True, slots=True)
class UpsertRuntimeTargetCommand:
    idempotency_key: str
    name: str
    connection_config: dict[str, Any]
    credential: str | None
    pool: RuntimePool
    runtime_type: RuntimeType = RuntimeType.JUPYTER
    actor_type: ActorType | None = None
    actor_id: str | None = None
    max_concurrent_executions: int | None = None


@dataclass(frozen=True, slots=True)
class DisableRuntimeTargetCommand:
    idempotency_key: str
    target_id: UUID
    actor_type: ActorType | None = None
    actor_id: str | None = None


@dataclass(frozen=True, slots=True)
class SetRuntimeTargetStateCommand:
    idempotency_key: str
    target_id: UUID
    desired_state: RuntimeTargetStatus
    actor_type: ActorType | None = None
    actor_id: str | None = None


@dataclass(frozen=True, slots=True)
class PurgeRuntimeTargetCommand:
    idempotency_key: str
    target_id: UUID
    confirmation_name: str
    actor_type: ActorType | None = None
    actor_id: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeTargetView:
    id: UUID
    name: str
    runtime_type: RuntimeType
    connection_config: dict[str, Any]
    pool: RuntimePool
    status: RuntimeTargetStatus
    enabled: bool
    max_concurrent_executions: int
    supported_profiles: tuple[str, ...]
    active_execution_count: int
    active_session_count: int | None
    admission_used_count: int
    session_count_observed_at: datetime | None
    session_count_fresh: bool
    last_health_check_at: datetime | None
    last_health_error: str | None
    resource_observed_at: datetime | None
    resource_last_check_at: datetime | None
    resource_last_error: str | None
    resource_fresh: bool
    resource_source: str | None
    resource_estimated: bool | None
    resource_process_count: int | None
    cpu_used_cores: float | None
    cpu_capacity_cores: float | None
    cpu_utilization: float | None
    memory_used_bytes: int | None
    memory_capacity_bytes: int | None
    memory_utilization: float | None
    resource_pressure_score: float | None
    resource_errors: tuple[str, ...]
    created_by_type: ActorType | None
    created_by: str | None
    updated_by_type: ActorType | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def accepting_new_executions(self) -> bool:
        return (
            self.enabled
            and self.status == RuntimeTargetStatus.ACTIVE
            and not self.admission_blocked
        )

    @property
    def admission_blocked(self) -> bool:
        return self.admission_used_count >= self.max_concurrent_executions

    @property
    def drain_complete(self) -> bool:
        return (
            self.status == RuntimeTargetStatus.DRAINING
            and self.admission_used_count == 0
        )

    @property
    def available_capacity(self) -> int:
        if not self.accepting_new_executions:
            return 0
        return max(
            0, self.max_concurrent_executions - self.admission_used_count
        )


@dataclass(frozen=True, slots=True)
class RuntimePoolView:
    runtime_type: RuntimeType
    pool: RuntimePool
    target_count: int
    enabled_target_count: int
    active_target_count: int
    draining_target_count: int
    offline_target_count: int
    configured_capacity: int
    schedulable_capacity: int
    active_execution_count: int
    available_capacity: int
    last_health_check_at: datetime | None

    @property
    def accepting_new_executions(self) -> bool:
        return self.available_capacity > 0

    @property
    def saturated(self) -> bool:
        return self.active_target_count > 0 and self.available_capacity == 0


@dataclass(frozen=True, slots=True)
class RuntimeTargetPurgeView:
    target_id: UUID
    name: str
    runtime_type: RuntimeType
    connection_config: dict[str, Any]
    pool: RuntimePool
    created_by_type: ActorType | None
    created_by: str | None
    updated_by_type: ActorType | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime


class RuntimeTargetManager(Protocol):
    async def upsert(
        self, command: UpsertRuntimeTargetCommand
    ) -> RuntimeTargetView: ...

    async def list(
        self,
        pool: RuntimePool | None = None,
        *,
        runtime_type: RuntimeType | None = None,
        status: RuntimeTargetStatus | None = None,
        enabled: bool | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[RuntimeTargetView]: ...

    async def pool_summaries(self) -> Sequence[RuntimePoolView]: ...

    async def get(self, target_id: UUID) -> RuntimeTargetView: ...

    async def probe(
        self,
        target_id: UUID,
        *,
        actor_type: ActorType | None = None,
        actor_id: str | None = None,
    ) -> RuntimeTargetView: ...

    async def disable(
        self, command: DisableRuntimeTargetCommand
    ) -> RuntimeTargetView: ...

    async def set_state(
        self, command: SetRuntimeTargetStateCommand
    ) -> RuntimeTargetView: ...

    async def purge(
        self, command: PurgeRuntimeTargetCommand
    ) -> RuntimeTargetPurgeView: ...
