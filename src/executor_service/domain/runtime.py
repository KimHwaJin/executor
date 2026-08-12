"""Runtime driver contracts shared by the application and infrastructure layers."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from executor_service.domain.enums import RuntimeType


class RuntimeDriverError(RuntimeError):
    """An execution runtime could not be reached or used."""


class RuntimeExecutionError(RuntimeDriverError):
    def __init__(self, message: str, outputs: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.outputs = outputs


@dataclass(frozen=True, slots=True)
class RuntimeExecutionResult:
    outputs: list[dict[str, Any]]
    execution_count: int | None


@dataclass(frozen=True, slots=True)
class RuntimeResourceMetric:
    used: float | int | None
    capacity: float | int | None
    utilization: float | None
    source: str | None
    estimated: bool | None
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RuntimeResourceObservation:
    observed_at: datetime
    process_count: int | None
    cpu: RuntimeResourceMetric
    memory: RuntimeResourceMetric


class RuntimeDriver(Protocol):
    async def close(self) -> None: ...

    async def status(self) -> dict[str, Any]: ...

    async def supported_profiles(self) -> list[str]: ...

    async def resource_status(self) -> RuntimeResourceObservation: ...

    async def start_session(self, profile: str, working_directory: str) -> str: ...

    async def interrupt_session(self, session_id: str) -> None: ...

    async def delete_session(self, session_id: str) -> None: ...

    async def session_exists(self, session_id: str) -> bool: ...

    async def execute(self, session_id: str, code: str) -> RuntimeExecutionResult: ...


class RuntimeDriverFactory(Protocol):
    def create(
        self,
        runtime_type: RuntimeType,
        connection_config: dict[str, Any],
        credential: str,
    ) -> RuntimeDriver: ...
