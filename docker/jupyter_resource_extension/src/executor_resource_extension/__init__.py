from __future__ import annotations

from typing import Any

from jupyter_server.utils import url_path_join  # ty: ignore[unresolved-import]

from executor_resource_extension.collector import ResourceCollector
from executor_resource_extension.handlers import ResourceStatusHandler


def _jupyter_server_extension_points() -> list[dict[str, str]]:
    return [{"module": "executor_resource_extension"}]


def _load_jupyter_server_extension(server_app: Any) -> None:
    web_app = server_app.web_app
    base_url = web_app.settings.get("base_url", "/")
    web_app.settings["executor_resource_collector"] = ResourceCollector.from_environment()
    web_app.add_handlers(
        ".*$",
        [
            (
                url_path_join(base_url, "executor", "resource-status"),
                ResourceStatusHandler,
            )
        ],
    )
    server_app.log.info("Executor Jupyter resource extension loaded")


load_jupyter_server_extension = _load_jupyter_server_extension
