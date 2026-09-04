"""Process entry point."""

import sys

import uvicorn

from executor_service.config import get_settings
from executor_service.container import ApplicationContainer
from executor_service.event_loop import run_async
from executor_service.interfaces.http.app import create_app
from executor_service.logging_config import configure_logging

settings = get_settings()
configure_logging(settings.log_config_file, settings.log_level)
container = ApplicationContainer(settings)
app = create_app(container)


def run() -> None:
    config = uvicorn.Config(
        "executor_service.main:app",
        host=settings.host,
        port=settings.port,
        # Keep the shared YAML handlers, formats and levels for Uvicorn too.
        log_config=None,
    )
    server = uvicorn.Server(config)
    if sys.platform == "win32":
        run_async(server.serve(), platform="win32")
    else:
        server.run()
