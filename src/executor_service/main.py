"""Process entry point."""

import sys

import uvicorn
from fastapi import FastAPI

from executor_service.container import ApplicationContainer
from executor_service.event_loop import run_async
from executor_service.infrastructure.db.logging import (
    install_database_error_filters,
)
from executor_service.interfaces.http.app import create_app
from executor_service.logging_setup import configure_logging
from executor_service.settings import get_settings

# Optional externalDocs entries use {"description": "...", "url": "..."}.
api_tags_meta = [
    {"name": "executions", "description": "Execution requests and results."},
    {"name": "runtime-targets", "description": "Runtime target management."},
    {"name": "maintenance", "description": "Executor maintenance controls."},
]


def create() -> FastAPI:
    """Build the app; Uvicorn calls this, so importing the module is inert."""
    settings = get_settings()
    configure_logging(settings)
    install_database_error_filters()
    container = ApplicationContainer(settings)
    return create_app(container, openapi_tags=api_tags_meta)


def run() -> None:
    settings = get_settings()
    config = uvicorn.Config(
        "executor_service.main:create",
        factory=True,
        host=settings.host,
        port=settings.port,
        # Keep configure_logging's setup (HCP or logger.yml) for Uvicorn too.
        log_config=None,
    )
    server = uvicorn.Server(config)
    if sys.platform == "win32":
        run_async(server.serve(), platform="win32")
    else:
        server.run()
