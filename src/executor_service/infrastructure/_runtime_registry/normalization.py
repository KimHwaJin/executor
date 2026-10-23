"""Validation and normalization of driver-owned Runtime configuration."""

from typing import Any
from urllib.parse import urlsplit

from executor_service.domain.enums import RuntimeType
from executor_service.domain.errors import RuntimeTargetConfigurationError

_DEFAULT_PORTS = {"http": 80, "https": 443}
MAX_CONNECTION_KEY_LENGTH = 2048


def normalize_connection_config(
    runtime_type: RuntimeType, connection_config: dict[str, Any]
) -> dict[str, Any]:
    """Validate connection data before it reaches persistent storage."""
    if runtime_type == RuntimeType.JUPYTER:
        endpoint = connection_config.get("endpoint")
        if set(connection_config) != {"endpoint"} or not isinstance(
            endpoint, str
        ):
            raise RuntimeTargetConfigurationError(
                "JUPYTER connection_config must contain only an http(s) "
                "endpoint."
            )
        return {"endpoint": normalize_endpoint(endpoint)}
    raise RuntimeTargetConfigurationError(
        f"Unsupported runtime_type: {runtime_type.value}"
    )


def normalize_endpoint(endpoint: str) -> str:
    """Spell one Runtime endpoint one way so duplicates can be detected."""
    try:
        parts = urlsplit(endpoint.strip())
        port = parts.port
    except ValueError as exc:
        raise RuntimeTargetConfigurationError(
            "JUPYTER endpoint is not a valid URL."
        ) from exc
    scheme = parts.scheme
    if (
        scheme not in _DEFAULT_PORTS
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise RuntimeTargetConfigurationError(
            "JUPYTER endpoint must be an http(s) URL without credentials, "
            "query, or fragment."
        )
    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        host = f"{host}:{port}"
    normalized = f"{scheme}://{host}{parts.path.rstrip('/')}"
    if len(normalized) > MAX_CONNECTION_KEY_LENGTH:
        raise RuntimeTargetConfigurationError("JUPYTER endpoint is too long.")
    return normalized
