from __future__ import annotations

from typing import Any

from jupyter_server.base.handlers import APIHandler  # ty: ignore[unresolved-import]
from tornado import web  # ty: ignore[unresolved-import]

from executor_resource_extension.collector import ResourceCollector


class ResourceStatusHandler(APIHandler):
    @web.authenticated
    def get(self) -> None:
        collector: ResourceCollector = self.settings["executor_resource_collector"]
        self.set_header("Cache-Control", "no-store")
        self.finish(collector.collect())

    def write_error(self, status_code: int, **kwargs: Any) -> None:
        self.set_header("Content-Type", "application/json")
        self.finish({"status": status_code, "message": "Resource status collection failed."})
