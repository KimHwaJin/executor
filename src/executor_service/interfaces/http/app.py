"""FastAPI host for MCP Streamable HTTP and operational endpoints."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from mcp.server.transport_security import TransportSecuritySettings

from executor_service.container import ApplicationContainer
from executor_service.domain.errors import (
    ArtifactRangeNotSatisfiableError,
    DomainError,
    ErrorCode,
)
from executor_service.interfaces.auth import CallerAuthenticationMiddleware
from executor_service.interfaces.http.body_limit import (
    RequestBodyLimitMiddleware,
)
from executor_service.interfaces.http.executions import build_execution_router
from executor_service.interfaces.http.maintenance import (
    build_maintenance_router,
)
from executor_service.interfaces.http.request_error_logging import (
    ExecutionRequestErrorLoggingMiddleware,
)
from executor_service.interfaces.http.runtime_targets import (
    build_runtime_target_router,
)
from executor_service.interfaces.mcp.server import build_mcp_server


def create_app(
    container: ApplicationContainer,
    *,
    openapi_tags: list[dict[str, Any]] | None = None,
) -> FastAPI:
    mcp_server = (
        build_mcp_server(
            container.execution_service,
            container.execution_queries,
            runtime_manager=container.runtime_registry,
            execution_spec_resolver=container.execution_spec_resolver,
            notebook_queries=container.notebook_queries,
            execution_results=container.execution_results,
            materialized_artifacts=container.materialized_artifacts,
        )
        if container.settings.mcp_enabled
        else None
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            await container.start()
            if mcp_server is None:
                yield
            else:
                # A mounted ASGI sub-application does not run its own lifespan.
                async with mcp_server.session_manager.run():
                    yield
        finally:
            await container.stop()

    app = FastAPI(
        title="Executor Service",
        version="0.1.0",
        description=(
            "Asynchronous Runtime execution REST facade. MCP Streamable HTTP is available "
            "at /mcp when MCP_ENABLED is true."
        ),
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        openapi_tags=openapi_tags,
        lifespan=lifespan,
    )
    app.state.container = container
    app.add_middleware(
        ExecutionRequestErrorLoggingMiddleware,
        enabled=container.settings.rest_error_body_logging_enabled,
        max_body_bytes=container.settings.rest_error_body_log_max_bytes,
    )
    app.add_middleware(
        CallerAuthenticationMiddleware, settings=container.settings
    )
    # Added last so it wraps everything, the MCP mount and the caller
    # check included: an oversized body is refused before it is read.
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=container.settings.request_body_max_bytes,
    )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = [
            {
                "location": list(error["loc"]),
                "message": error["msg"],
                "type": error["type"],
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "error": {
                    "code": ErrorCode.REQUEST_VALIDATION_ERROR,
                    "message": "Request validation failed.",
                    "details": details,
                }
            },
        )

    @app.exception_handler(DomainError)
    async def domain_error_handler(
        _request: Request, exc: DomainError
    ) -> JSONResponse:
        if isinstance(exc, ArtifactRangeNotSatisfiableError):
            return JSONResponse(
                status_code=exc.http_status,
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Range": f"bytes */{exc.size_bytes}",
                },
                content={
                    "error": {
                        "code": exc.code,
                        "message": str(exc),
                    }
                },
            )
        return JSONResponse(
            status_code=exc.http_status,
            content={
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                }
            },
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(
        _request: Request, exc: Exception
    ) -> JSONResponse:
        logging.getLogger(__name__).exception(
            "Unhandled request error", exc_info=exc
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "code": ErrorCode.INTERNAL_ERROR,
                    "message": "An internal error occurred.",
                }
            },
        )

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(response: Response) -> dict[str, object]:
        checks = await container.readiness()
        ready = all(checks.values())
        if not ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "ready" if ready else "not_ready",
            "checks": checks,
        }

    @app.get("/workerz", include_in_schema=False)
    async def workerz() -> dict[str, object]:
        worker = container.execution_worker
        return {
            "worker_id": worker.worker_id,
            "state": worker.lifecycle_state,
            "accepting_new_executions": worker.accepting_work,
            "active_execution_count": worker.active_job_count,
            # Reported for operators; event publication retries on its own.
            "redis_reachable": await container.redis_reachable(),
            "startup_reconciliation": {
                "completed_at": (
                    worker.startup_reconciliation_completed_at.isoformat()
                    if worker.startup_reconciliation_completed_at is not None
                    else None
                ),
                "recovered_execution_count": (
                    worker.startup_recovered_execution_count
                ),
                "runtime_cleanup_target_count": (
                    worker.startup_cleanup_target_count
                ),
            },
        }

    app.include_router(build_execution_router(container))
    app.include_router(build_runtime_target_router(container))
    app.include_router(build_maintenance_router(container))

    if mcp_server is not None:
        transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(container.settings.mcp_allowed_hosts),
            allowed_origins=list(container.settings.mcp_allowed_origins),
        )
        # Mounted at the root because a "/mcp" mount answers POST /mcp with a
        # 307 to "/mcp/". Registered last so every route above wins.
        app.mount(
            "/",
            mcp_server.streamable_http_app(
                streamable_http_path="/mcp",
                json_response=True,
                stateless_http=True,
                transport_security=transport_security,
            ),
        )
    return app
