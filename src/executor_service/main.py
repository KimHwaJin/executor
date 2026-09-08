"""Process entry point."""

import sys

import uvicorn

from executor_service.config import api_tags_meta
from executor_service.container import ApplicationContainer
from executor_service.event_loop import run_async
from executor_service.infrastructure.db.logging import (
    install_database_error_filters,
)
from executor_service.interfaces.http.app import create_app
from executor_service.settings import get_settings

settings = get_settings()
install_database_error_filters()
container = ApplicationContainer(settings)
app = create_app(container, openapi_tags=api_tags_meta)


def run() -> None:
    config = uvicorn.Config(
        "executor_service.main:app",
        host=settings.host,
        port=settings.port,
        # Keep the deployment Config's logging setup for Uvicorn too.
        log_config=None,
    )
    server = uvicorn.Server(config)
    if sys.platform == "win32":
        run_async(server.serve(), platform="win32")
    else:
        server.run()
