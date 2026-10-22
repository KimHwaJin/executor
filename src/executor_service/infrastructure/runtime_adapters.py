"""Runtime adapters: everything Executor knows about one Runtime type.

Adding a Runtime type means adding a RuntimeType value, a driver that
implements the RuntimeDriver contract and an adapter registered in
ADAPTER_TYPES. Nothing outside this module branches on the Runtime type.
"""

from typing import Any, ClassVar, Protocol

from executor_service.domain.enums import RuntimeType
from executor_service.domain.errors import RuntimeTargetConfigurationError
from executor_service.domain.runtime import (
    RuntimeCapabilities,
    RuntimeDriver,
    RuntimeDriverError,
    RuntimeOffering,
)
from executor_service.infrastructure.jupyter import JupyterRuntimeDriver
from executor_service.settings import Settings


class RuntimeAdapter(Protocol):
    runtime_type: ClassVar[RuntimeType]
    capabilities: ClassVar[RuntimeCapabilities]
    # Scheme of the URI recorded for Artifacts on this Runtime's storage.
    artifact_uri_scheme: ClassVar[str]

    @staticmethod
    def normalize_connection_config(
        connection_config: dict[str, Any],
    ) -> dict[str, Any]:
        """Validate connection data before it reaches persistent storage."""
        ...

    def allowed_profiles(self) -> tuple[str, ...]: ...

    def create_driver(
        self, connection_config: dict[str, Any], credential: str
    ) -> RuntimeDriver: ...


class JupyterRuntimeAdapter:
    runtime_type: ClassVar[RuntimeType] = RuntimeType.JUPYTER
    capabilities: ClassVar[RuntimeCapabilities] = RuntimeCapabilities(
        stateful_sessions=True,
        execution_journal=True,
        durable_workspace=True,
    )
    artifact_uri_scheme: ClassVar[str] = "jupyter-pv"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @staticmethod
    def normalize_connection_config(
        connection_config: dict[str, Any],
    ) -> dict[str, Any]:
        endpoint = connection_config.get("endpoint")
        if (
            set(connection_config) != {"endpoint"}
            or not isinstance(endpoint, str)
            or not endpoint.startswith(("http://", "https://"))
        ):
            raise RuntimeTargetConfigurationError(
                "JUPYTER connection_config must contain only an http(s) "
                "endpoint."
            )
        return {"endpoint": endpoint.rstrip("/")}

    def allowed_profiles(self) -> tuple[str, ...]:
        return self._settings.runtime_allowed_profiles

    def create_driver(
        self, connection_config: dict[str, Any], credential: str
    ) -> RuntimeDriver:
        endpoint = connection_config.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint:
            raise RuntimeDriverError(
                "JUPYTER Runtime Target requires connection_config.endpoint."
            )
        return JupyterRuntimeDriver(
            endpoint,
            credential,
            self._settings.jupyter_request_timeout_seconds,
            self._settings.jupyter_storage_timeout_seconds,
            self._settings.runtime_max_output_message_bytes,
            stream_reconnect_seconds=(
                self._settings.jupyter_stream_reconnect_seconds
            ),
        )


ADAPTER_TYPES: dict[RuntimeType, type[JupyterRuntimeAdapter]] = {
    RuntimeType.JUPYTER: JupyterRuntimeAdapter,
}


def _adapter_type(runtime_type: RuntimeType) -> type[RuntimeAdapter]:
    adapter = ADAPTER_TYPES.get(runtime_type)
    if adapter is None:
        raise RuntimeTargetConfigurationError(
            f"Unsupported runtime_type: {runtime_type.value}"
        )
    return adapter


def normalize_connection_config(
    runtime_type: RuntimeType, connection_config: dict[str, Any]
) -> dict[str, Any]:
    return _adapter_type(runtime_type).normalize_connection_config(
        connection_config
    )


def runtime_capabilities(runtime_type: RuntimeType) -> RuntimeCapabilities:
    return _adapter_type(runtime_type).capabilities


def artifact_uri(runtime_type: RuntimeType, path: str) -> str:
    return f"{_adapter_type(runtime_type).artifact_uri_scheme}:///{path}"


class RuntimeAdapterRegistry:
    """The configured adapters; creates drivers for stored Runtime Targets."""

    def __init__(self, settings: Settings) -> None:
        self._adapters: dict[RuntimeType, RuntimeAdapter] = {
            runtime_type: adapter(settings)
            for runtime_type, adapter in ADAPTER_TYPES.items()
        }

    def adapter(self, runtime_type: RuntimeType) -> RuntimeAdapter:
        adapter = self._adapters.get(runtime_type)
        if adapter is None:
            raise RuntimeDriverError(
                f"Unsupported runtime_type: {runtime_type}"
            )
        return adapter

    def create(
        self,
        runtime_type: RuntimeType,
        connection_config: dict[str, Any],
        credential: str,
    ) -> RuntimeDriver:
        return self.adapter(runtime_type).create_driver(
            connection_config, credential
        )

    def offerings(self) -> dict[RuntimeType, RuntimeOffering]:
        return {
            runtime_type: RuntimeOffering(
                profiles=adapter.allowed_profiles(),
                capabilities=adapter.capabilities,
            )
            for runtime_type, adapter in self._adapters.items()
        }
