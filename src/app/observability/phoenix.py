"""Optional Phoenix/OpenTelemetry initialization."""

from __future__ import annotations

from typing import Any

from agent_config import AgentSettings

# Phoenix 패키지를 설치한 뒤 아래 import의 주석을 해제하세요.
from phoenix.otel import register


_tracer_provider: Any | None = None


def setup_phoenix(settings: AgentSettings) -> Any | None:
    """Register Phoenix once when a collector endpoint is configured."""
    global _tracer_provider

    if not settings.phoenix_endpoint:
        return None
    if _tracer_provider is not None:
        return _tracer_provider
    
    headers = (
    {"Authorization": f"Bearer {settings.phoenix_api_key}"}
    if settings.phoenix_api_key
    else None
    )
    # 위 import와 아래 register 호출의 주석을 해제하세요.
    _tracer_provider = register(
        endpoint=settings.phoenix_endpoint,
        project_name=settings.phoenix_project_name,
        headers=headers,
        auto_instrument=True,
        batch=False,
    )
    return _tracer_provider

    raise RuntimeError(
        "PHOENIX_ENDPOINT가 설정되어 있습니다. "
        "app/observability/phoenix.py의 Phoenix 연동 주석을 해제하세요."
    )


def shutdown_phoenix() -> None:
    """Flush and close the registered provider, if any."""
    global _tracer_provider

    provider = _tracer_provider
    _tracer_provider = None
    if provider is None:
        return
    force_flush = getattr(provider, "force_flush", None)
    if callable(force_flush):
        force_flush()
    shutdown = getattr(provider, "shutdown", None)
    if callable(shutdown):
        shutdown()
