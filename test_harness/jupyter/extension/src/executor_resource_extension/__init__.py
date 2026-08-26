from __future__ import annotations

from typing import Any


def _jupyter_server_extension_points() -> list[dict[str, str]]:
    return [{"module": "executor_resource_extension"}]


def _load_jupyter_server_extension(server_app: Any) -> None:
    from jupyter_server.utils import (  # ty: ignore[unresolved-import]
        url_path_join,
    )

    from executor_resource_extension.collector import ResourceCollector
    from executor_resource_extension.handlers import (
        ArtifactSnapshotHandler,
        FileContentHandler,
        FileMetadataHandler,
        ManifestReadHandler,
        NotebookPrepareHandler,
        NotebookProjectHandler,
        ResourceStatusHandler,
        WorkspacePrepareHandler,
    )
    from executor_resource_extension.storage import RuntimeStorage

    web_app = server_app.web_app
    base_url = web_app.settings.get("base_url", "/")
    web_app.settings["executor_resource_collector"] = (
        ResourceCollector.from_environment()
    )
    web_app.settings["executor_runtime_storage"] = RuntimeStorage(
        server_app.root_dir
    )
    web_app.add_handlers(
        ".*$",
        [
            (
                url_path_join(base_url, "executor", "resource-status"),
                ResourceStatusHandler,
            ),
            (
                url_path_join(
                    base_url, "executor", "storage", "workspaces", "prepare"
                ),
                WorkspacePrepareHandler,
            ),
            (
                url_path_join(
                    base_url, "executor", "storage", "artifacts", "snapshot"
                ),
                ArtifactSnapshotHandler,
            ),
            (
                url_path_join(
                    base_url, "executor", "storage", "files", "metadata"
                ),
                FileMetadataHandler,
            ),
            (
                url_path_join(
                    base_url, "executor", "storage", "files", "content"
                ),
                FileContentHandler,
            ),
            (
                url_path_join(
                    base_url, "executor", "storage", "manifests", "read"
                ),
                ManifestReadHandler,
            ),
            (
                url_path_join(
                    base_url, "executor", "storage", "notebooks", "prepare"
                ),
                NotebookPrepareHandler,
            ),
            (
                url_path_join(
                    base_url, "executor", "storage", "notebooks", "project"
                ),
                NotebookProjectHandler,
            ),
        ],
    )
    server_app.log.info("Executor Jupyter resource extension loaded")


load_jupyter_server_extension = _load_jupyter_server_extension
