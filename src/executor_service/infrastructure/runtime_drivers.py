"""Factory for runtime-specific execution adapters."""

from typing import Any

from executor_service.config import Settings
from executor_service.domain.enums import RuntimeType
from executor_service.domain.runtime import RuntimeDriver, RuntimeDriverError
from executor_service.infrastructure.jupyter import JupyterRuntimeDriver


class ConfiguredRuntimeDriverFactory:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def create(
        self,
        runtime_type: RuntimeType,
        connection_config: dict[str, Any],
        credential: str,
    ) -> RuntimeDriver:
        if runtime_type == RuntimeType.JUPYTER:
            endpoint = connection_config.get("endpoint")
            if not isinstance(endpoint, str) or not endpoint:
                raise RuntimeDriverError(
                    "JUPYTER Runtime Target requires connection_config.endpoint."
                )
            return JupyterRuntimeDriver(
                endpoint,
                credential,
                self._settings.jupyter_request_timeout_seconds,
            )
        raise RuntimeDriverError(f"Unsupported runtime_type: {runtime_type}")
