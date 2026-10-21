"""Validation and normalization of driver-owned Runtime configuration."""

from typing import Any

from executor_service.domain.enums import RuntimeType
from executor_service.domain.errors import RuntimeTargetConfigurationError


def normalize_connection_config(
    runtime_type: RuntimeType, connection_config: dict[str, Any]
) -> dict[str, Any]:
    """Validate connection data before it reaches persistent storage."""
    if runtime_type == RuntimeType.JUPYTER:
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
    raise RuntimeTargetConfigurationError(
        f"Unsupported runtime_type: {runtime_type.value}"
    )
