"""FastAPI host for MCP Streamable HTTP and operational endpoints."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response, status
from mcp.server.transport_security import TransportSecuritySettings
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from executor_service.container import ApplicationContainer
from executor_service.interfaces.mcp.server import build_mcp_server


def create_app(container: ApplicationContainer) -> FastAPI:
    mcp_server = build_mcp_server(
        container.execution_service,
        container.jupyter_registry,
        container.execution_queries,
    )
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(container.settings.mcp_allowed_hosts),
        allowed_origins=list(container.settings.mcp_allowed_origins),
    )
    mcp_app = mcp_server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=transport_security,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await container.start()
        try:
            # A mounted ASGI sub-application does not run its own lifespan.
            async with mcp_server.session_manager.run():
                yield
        finally:
            await container.stop()

    app = FastAPI(
        title="Executor Service",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.container = container
    app.state.mcp_server = mcp_server

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(response: Response) -> dict[str, object]:
        checks = await container.readiness()
        ready = all(checks.values())
        if not ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "ready" if ready else "not_ready", "checks": checks}

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # Register this catch-all mount last so operational routes remain reachable.
    app.mount("/", mcp_app)
    return app


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
