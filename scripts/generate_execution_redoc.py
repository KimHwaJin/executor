"""Build a self-contained ReDoc HTML for the Execution REST API."""

from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from executor_service.config import Settings
from executor_service.container import ApplicationContainer
from executor_service.interfaces.http.app import create_app

HTTP_METHODS = {
    "delete",
    "get",
    "head",
    "options",
    "patch",
    "post",
    "put",
    "trace",
}
EXECUTION_TAG = "executions"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--redoc-js",
        required=True,
        type=Path,
        help="Local redoc.standalone.js file to embed.",
    )
    parser.add_argument(
        "--output",
        default=Path("dev_docs/execution-api-redoc.html"),
        type=Path,
        help="Destination HTML path.",
    )
    return parser.parse_args()


def _execution_openapi() -> dict[str, Any]:
    settings = Settings(runtime_enabled=False, tracing_enabled=False)
    app = create_app(ApplicationContainer(settings))
    source = app.openapi()
    selected_paths: dict[str, Any] = {}
    for path, path_item in source.get("paths", {}).items():
        selected_item = {
            key: deepcopy(value)
            for key, value in path_item.items()
            if key not in HTTP_METHODS
        }
        for method, operation in path_item.items():
            if method in HTTP_METHODS and EXECUTION_TAG in operation.get(
                "tags", []
            ):
                selected_item[method] = deepcopy(operation)
        if any(method in selected_item for method in HTTP_METHODS):
            selected_paths[path] = selected_item

    document: dict[str, Any] = {
        "openapi": source["openapi"],
        "info": {
            **deepcopy(source["info"]),
            "title": "Executor Execution REST API",
            "description": (
                "Execution 제출, 제어, 상태·결과·이력 및 산출물 조회를 위한 "
                "Executor REST API 전용 문서입니다."
            ),
        },
        "paths": selected_paths,
        "tags": [
            tag
            for tag in deepcopy(source.get("tags", []))
            if tag.get("name") == EXECUTION_TAG
        ],
    }
    if "servers" in source:
        document["servers"] = deepcopy(source["servers"])
    if "security" in source:
        document["security"] = deepcopy(source["security"])

    document["components"] = _referenced_components(source, document)
    return document


def _referenced_components(
    source: dict[str, Any], document: dict[str, Any]
) -> dict[str, Any]:
    source_components = source.get("components", {})
    selected: dict[str, dict[str, Any]] = {}
    pending = list(_find_component_refs(document))
    visited: set[str] = set()

    while pending:
        ref = pending.pop()
        if ref in visited:
            continue
        visited.add(ref)
        parts = ref.removeprefix("#/components/").split("/")
        if len(parts) != 2:
            continue
        component_type, name = parts
        component = source_components.get(component_type, {}).get(name)
        if component is None:
            continue
        selected.setdefault(component_type, {})[name] = deepcopy(component)
        pending.extend(_find_component_refs(component))

    return {
        component_type: dict(sorted(values.items()))
        for component_type, values in sorted(selected.items())
    }


def _find_component_refs(value: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, dict):
        ref = value.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/"):
            refs.add(ref)
        for child in value.values():
            refs.update(_find_component_refs(child))
    elif isinstance(value, list):
        for child in value:
            refs.update(_find_component_refs(child))
    return refs


def _render_html(spec: dict[str, Any], redoc_js: str) -> str:
    encoded_spec = json.dumps(
        spec,
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Executor Execution REST API</title>
  <style>
    html, body {{ margin: 0; padding: 0; }}
    body {{ font-family: Arial, "Apple SD Gothic Neo", sans-serif; }}
  </style>
</head>
<body>
  <div id="redoc-container"></div>
  <script>{redoc_js}</script>
  <script>
    const executionOpenApi = {encoded_spec};
    Redoc.init(executionOpenApi, {{
      hideDownloadButton: false,
      nativeScrollbars: true,
      pathInMiddlePanel: true,
      theme: {{ typography: {{ fontFamily: 'Arial, sans-serif' }} }}
    }}, document.getElementById('redoc-container'));
  </script>
</body>
</html>
"""


def main() -> None:
    args = _parse_args()
    redoc_js = args.redoc_js.read_text(encoding="utf-8")
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        _render_html(_execution_openapi(), redoc_js),
        encoding="utf-8",
    )
    print(output)


if __name__ == "__main__":
    main()
