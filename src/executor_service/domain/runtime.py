"""Runtime driver contracts shared by the application and infrastructure layers."""

from dataclasses import dataclass
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


class RuntimeDriver(Protocol):
    async def close(self) -> None: ...

    async def status(self) -> dict[str, Any]: ...

    async def supported_profiles(self) -> list[str]: ...

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
