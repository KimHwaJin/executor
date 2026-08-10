"""Process entry point."""

import sys

import uvicorn

from executor_service.config import get_settings
from executor_service.container import ApplicationContainer
from executor_service.event_loop import run_async
from executor_service.interfaces.http.app import configure_logging, create_app

settings = get_settings()
configure_logging(settings.log_level)
container = ApplicationContainer(settings)
app = create_app(container)


def run() -> None:
    config = uvicorn.Config(
        "executor_service.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )
    server = uvicorn.Server(config)
    if sys.platform == "win32":
        run_async(server.serve(), platform="win32")
    else:
        server.run()
