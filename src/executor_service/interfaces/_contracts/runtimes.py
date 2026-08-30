"""Runtime target and pool transport contracts."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field, SecretStr, model_validator

from executor_service.application.pagination import Page
from executor_service.application.runtime_targets import (
    RuntimePoolView,
    RuntimeTargetView,
    UpsertRuntimeTargetCommand,
)
from executor_service.domain.enums import (
    RuntimePool,
    RuntimeTargetStatus,
    RuntimeType,
)
from executor_service.interfaces._contracts.common import (
    ActorInput,
    AuditFields,
    ContractModel,
    PageResponse,
)


class RuntimeTargetUpsertRequest(ContractModel):
    idempotency_key: str = Field(min_length=1, max_length=255)
    name: str = Field(
        min_length=1, max_length=255, pattern=r"^[a-zA-Z0-9._-]+$"
    )
    runtime_type: RuntimeType
    connection_config: dict[str, Any]
    credential: SecretStr | None = None
    pool: RuntimePool
    max_concurrent_executions: int | None = Field(default=None, ge=1, le=1000)
    actor: ActorInput

    @model_validator(mode="after")
    def validate_connection_config(self) -> "RuntimeTargetUpsertRequest":
        if self.runtime_type == RuntimeType.JUPYTER:
            endpoint = self.connection_config.get("endpoint")
            if set(self.connection_config) != {"endpoint"} or not isinstance(
                endpoint, str
            ):
                raise ValueError(
                    "JUPYTER connection_config must contain only a non-empty endpoint."
                )
            if not endpoint.startswith(("http://", "https://")):
                raise ValueError("JUPYTER endpoint must use http or https.")
        return self

    def to_command(self) -> UpsertRuntimeTargetCommand:
        return UpsertRuntimeTargetCommand(
            idempotency_key=self.idempotency_key,
            name=self.name,
            runtime_type=self.runtime_type,
            connection_config=self.connection_config,
            credential=(
                self.credential.get_secret_value() if self.credential else None
            ),
            pool=self.pool,
            max_concurrent_executions=self.max_concurrent_executions,
            actor_type=self.actor.type,
            actor_id=self.actor.id,
        )


class RuntimeTargetRuntime(ContractModel):
    type: RuntimeType
    pool: RuntimePool
    connection_config: dict[str, Any]
    supported_profiles: list[str]


class RuntimeTargetState(ContractModel):
    status: RuntimeTargetStatus
    enabled: bool
    accepting_new_executions: bool
    drain_complete: bool


class RuntimeTargetCapacity(ContractModel):
    max_concurrent_executions: int
    active_execution_count: int
    active_session_count: int | None
    admission_used_count: int
    available_capacity: int
    admission_blocked: bool
    session_count_observed_at: datetime | None
    session_count_fresh: bool


class RuntimeTargetHealth(ContractModel):
    last_check_at: datetime | None
    last_error: str | None


class CpuResources(ContractModel):
    used_cores: float | None
    capacity_cores: float | None
    utilization: float | None


class MemoryResources(ContractModel):
    used_bytes: int | None
    capacity_bytes: int | None
    utilization: float | None


class RuntimeTargetResources(ContractModel):
    observed_at: datetime | None
    last_check_at: datetime | None
    last_error: str | None
    fresh: bool
    source: str | None
    estimated: bool | None
    process_count: int | None
    pressure_score: float | None
    cpu: CpuResources
    memory: MemoryResources
    errors: list[str]


class RuntimeTargetResponse(AuditFields):
    target_id: UUID
    name: str
    runtime: RuntimeTargetRuntime
    state: RuntimeTargetState
    capacity: RuntimeTargetCapacity
    health: RuntimeTargetHealth
    resources: RuntimeTargetResources

    @classmethod
    def from_view(cls, view: RuntimeTargetView) -> "RuntimeTargetResponse":
        return cls(
            target_id=view.id,
            name=view.name,
            runtime=RuntimeTargetRuntime(
                type=view.runtime_type,
                pool=view.pool,
                connection_config=view.connection_config,
                supported_profiles=list(view.supported_profiles),
            ),
            state=RuntimeTargetState(
                status=view.status,
                enabled=view.enabled,
                accepting_new_executions=view.accepting_new_executions,
                drain_complete=view.drain_complete,
            ),
            capacity=RuntimeTargetCapacity(
                max_concurrent_executions=view.max_concurrent_executions,
                active_execution_count=view.active_execution_count,
                active_session_count=view.active_session_count,
                admission_used_count=view.admission_used_count,
                available_capacity=view.available_capacity,
                admission_blocked=view.admission_blocked,
                session_count_observed_at=view.session_count_observed_at,
                session_count_fresh=view.session_count_fresh,
            ),
            health=RuntimeTargetHealth(
                last_check_at=view.last_health_check_at,
                last_error=view.last_health_error,
            ),
            resources=RuntimeTargetResources(
                observed_at=view.resource_observed_at,
                last_check_at=view.resource_last_check_at,
                last_error=view.resource_last_error,
                fresh=view.resource_fresh,
                source=view.resource_source,
                estimated=view.resource_estimated,
                process_count=view.resource_process_count,
                pressure_score=view.resource_pressure_score,
                cpu=CpuResources(
                    used_cores=view.cpu_used_cores,
                    capacity_cores=view.cpu_capacity_cores,
                    utilization=view.cpu_utilization,
                ),
                memory=MemoryResources(
                    used_bytes=view.memory_used_bytes,
                    capacity_bytes=view.memory_capacity_bytes,
                    utilization=view.memory_utilization,
                ),
                errors=list(view.resource_errors),
            ),
            created_by_type=view.created_by_type,
            created_by=view.created_by,
            updated_by_type=view.updated_by_type,
            updated_by=view.updated_by,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )


class RuntimeTargetPageResponse(PageResponse):
    items: list[RuntimeTargetResponse]

    @classmethod
    def from_page(
        cls, page: Page[RuntimeTargetView]
    ) -> "RuntimeTargetPageResponse":
        return cls(
            items=[
                RuntimeTargetResponse.from_view(item) for item in page.items
            ],
            next_cursor=page.next_cursor,
            has_more=page.next_cursor is not None,
        )


class RuntimePoolIdentity(ContractModel):
    type: RuntimeType
    pool: RuntimePool


class RuntimePoolTargets(ContractModel):
    total: int
    enabled: int
    active: int
    draining: int
    offline: int


class RuntimePoolCapacity(ContractModel):
    configured: int
    schedulable: int
    reserved_execution_count: int
    available: int


class RuntimePoolState(ContractModel):
    accepting_new_executions: bool
    saturated: bool


class RuntimePoolResponse(ContractModel):
    runtime: RuntimePoolIdentity
    targets: RuntimePoolTargets
    capacity: RuntimePoolCapacity
    state: RuntimePoolState

    @classmethod
    def from_view(cls, view: RuntimePoolView) -> "RuntimePoolResponse":
        return cls(
            runtime=RuntimePoolIdentity(
                type=view.runtime_type, pool=view.pool
            ),
            targets=RuntimePoolTargets(
                total=view.target_count,
                enabled=view.enabled_target_count,
                active=view.active_target_count,
                draining=view.draining_target_count,
                offline=view.offline_target_count,
            ),
            capacity=RuntimePoolCapacity(
                configured=view.configured_capacity,
                schedulable=view.schedulable_capacity,
                reserved_execution_count=view.active_execution_count,
                available=view.available_capacity,
            ),
            state=RuntimePoolState(
                accepting_new_executions=view.accepting_new_executions,
                saturated=view.saturated,
            ),
        )


class RuntimePoolPageResponse(ContractModel):
    items: list[RuntimePoolResponse]
