"""FastAPI host for MCP Streamable HTTP and operational endpoints."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from mcp.server.transport_security import TransportSecuritySettings

from executor_service.container import ApplicationContainer
from executor_service.domain.errors import (
    DomainError,
    ErrorCode,
    ExecutionArtifactNotFoundError,
    ExecutionNotFoundError,
    ExecutionVersionConflictError,
    IdempotencyConflictError,
    InvalidCursorError,
    InvalidExecutionSpecError,
    InvalidStateTransitionError,
    PersistenceConflictError,
    RuntimeTargetNotFoundError,
    RuntimeTargetPurgeConflictError,
    UnsupportedRuntimeProfileError,
)
from executor_service.interfaces.http.executions import build_execution_router
from executor_service.interfaces.http.runtime_targets import build_runtime_target_router
from executor_service.interfaces.mcp.server import build_mcp_server
from executor_service.tracing import TraceContextMiddleware


def create_app(container: ApplicationContainer) -> FastAPI:
    mcp_server = build_mcp_server(
        container.execution_service,
        container.runtime_registry,
        container.execution_queries,
        container.tracing,
        container.execution_spec_resolver,
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
        description=(
            "Asynchronous Runtime execution REST facade. MCP Streamable HTTP remains available "
            "at /mcp."
        ),
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )
    app.state.container = container
    app.state.mcp_server = mcp_server
    app.add_middleware(TraceContextMiddleware, tracing=container.tracing)

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
    async def domain_error_handler(_request: Request, exc: DomainError) -> JSONResponse:
        if isinstance(
            exc,
            (
                ExecutionNotFoundError,
                ExecutionArtifactNotFoundError,
                RuntimeTargetNotFoundError,
            ),
        ):
            http_status = status.HTTP_404_NOT_FOUND
        elif isinstance(
            exc,
            (InvalidCursorError, InvalidExecutionSpecError, UnsupportedRuntimeProfileError),
        ):
            http_status = status.HTTP_422_UNPROCESSABLE_CONTENT
        elif isinstance(
            exc,
            (
                ExecutionVersionConflictError,
                IdempotencyConflictError,
                InvalidStateTransitionError,
                RuntimeTargetPurgeConflictError,
                PersistenceConflictError,
            ),
        ):
            http_status = status.HTTP_409_CONFLICT
        else:
            http_status = status.HTTP_400_BAD_REQUEST
        return JSONResponse(
            status_code=http_status,
            content={
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                }
            },
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_request: Request, exc: Exception) -> JSONResponse:
        logging.getLogger(__name__).exception("Unhandled request error", exc_info=exc)
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
        return {"status": "ready" if ready else "not_ready", "checks": checks}

    @app.get("/workerz", include_in_schema=False)
    async def workerz() -> dict[str, object]:
        worker = container.execution_worker
        return {
            "state": worker.lifecycle_state,
            "accepting_new_executions": worker.accepting_work,
            "active_execution_count": worker.active_job_count,
        }

    app.include_router(build_execution_router(container))
    app.include_router(build_runtime_target_router(container))

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
