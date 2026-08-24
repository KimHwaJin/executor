"""Shared helpers for local Executor load and soak validation scripts."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

import httpx
from mcp import Client


@dataclass(frozen=True, slots=True)
class LocalRuntimeSpec:
    name: str
    endpoint: str
    token: str
    pool: str
    capacity: int = 1


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    value = int(os.getenv(name, str(default)))
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}.")
    return value


def env_float(
    name: str, default: float, *, minimum: float | None = None
) -> float:
    value = float(os.getenv(name, str(default)))
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}.")
    return value


def executor_http_url() -> str:
    return os.getenv(
        "LOCAL_TEST_EXECUTOR_URL", "http://127.0.0.1:8000"
    ).rstrip("/")


def executor_mcp_url() -> str:
    return os.getenv("EXECUTOR_MCP_URL", f"{executor_http_url()}/mcp")


def local_runtime_specs() -> tuple[LocalRuntimeSpec, ...]:
    """Return the Compose-internal fleet by default, or host endpoints for native Executor."""

    topology = (
        os.getenv("LOCAL_TEST_EXECUTOR_TOPOLOGY", "compose").strip().lower()
    )
    if topology not in {"compose", "native"}:
        raise ValueError(
            "LOCAL_TEST_EXECUTOR_TOPOLOGY must be 'compose' or 'native'."
        )
    endpoints = (
        (
            "http://jupyter:8888",
            "http://jupyter-secondary:8888",
            "http://jupyter-batch-primary:8888",
            "http://jupyter-batch-secondary:8888",
        )
        if topology == "compose"
        else (
            "http://127.0.0.1:8888",
            "http://127.0.0.1:8889",
            "http://127.0.0.1:8890",
            "http://127.0.0.1:8891",
        )
    )
    return (
        LocalRuntimeSpec(
            name="local-jupyter",
            endpoint=os.getenv("LOCAL_JUPYTER_ENDPOINT", endpoints[0]),
            token=os.getenv("JUPYTER_TOKEN", "change-me-local-only"),
            pool="INTERACTIVE",
        ),
        LocalRuntimeSpec(
            name="local-jupyter-secondary",
            endpoint=os.getenv(
                "LOCAL_JUPYTER_SECONDARY_ENDPOINT", endpoints[1]
            ),
            token=os.getenv(
                "JUPYTER_SECONDARY_TOKEN", "change-me-secondary-local-only"
            ),
            pool="INTERACTIVE",
        ),
        LocalRuntimeSpec(
            name="local-jupyter-batch-primary",
            endpoint=os.getenv(
                "LOCAL_JUPYTER_BATCH_PRIMARY_ENDPOINT", endpoints[2]
            ),
            token=os.getenv(
                "JUPYTER_BATCH_PRIMARY_TOKEN",
                "change-me-batch-primary-local-only",
            ),
            pool="BATCH",
        ),
        LocalRuntimeSpec(
            name="local-jupyter-batch-secondary",
            endpoint=os.getenv(
                "LOCAL_JUPYTER_BATCH_SECONDARY_ENDPOINT", endpoints[3]
            ),
            token=os.getenv(
                "JUPYTER_BATCH_SECONDARY_TOKEN",
                "change-me-batch-secondary-local-only",
            ),
            pool="BATCH",
        ),
    )


async def required_tool_result(
    client: Client,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    result = await client.call_tool(tool_name, arguments)
    if result.is_error or result.structured_content is None:
        raise RuntimeError(f"{tool_name} failed: {result.content}")
    return result.structured_content


async def execution_output_items(
    client: Client, execution_id: str
) -> list[dict[str, Any]]:
    """Read every normalized output descriptor without result-body duplication."""
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page = await required_tool_result(
            client,
            "execution_output_list",
            {
                "execution_id": execution_id,
                "cursor": cursor,
                "limit": 200,
            },
        )
        items.extend(page["items"])
        cursor = page.get("next_cursor")
        if cursor is None:
            return items


async def execution_output_content(
    client: Client,
    execution_id: str,
    output_id: str,
    representation_id: str,
) -> bytes:
    """Resolve an MCP-inline or HTTP-streamed output representation."""
    descriptor = await required_tool_result(
        client,
        "execution_output_content_get",
        {
            "execution_id": execution_id,
            "output_id": output_id,
            "representation_id": representation_id,
        },
    )
    if descriptor["delivery"] == "INLINE":
        return (descriptor.get("content") or "").encode("utf-8")
    async with httpx.AsyncClient(
        base_url=executor_http_url(), timeout=60
    ) as http:
        response = await http.get(descriptor["content_url"])
        response.raise_for_status()
        return response.content


async def execution_stream_text(client: Client, execution_id: str) -> str:
    """Read ordered stdout/stderr text from normalized output references."""
    chunks: list[str] = []
    for output in await execution_output_items(client, execution_id):
        if output["kind"] != "STREAM":
            continue
        for representation in output["representations"]:
            if representation["media_type"] != "text/plain":
                continue
            content = await execution_output_content(
                client,
                execution_id,
                output["output_id"],
                representation["representation_id"],
            )
            chunks.append(content.decode("utf-8"))
    return "".join(chunks)


async def register_local_runtime_targets(
    client: Client,
    *,
    run_id: str,
    include_batch: bool = True,
    include_secondary: bool = True,
) -> list[dict[str, Any]]:
    selected = [
        spec
        for spec in local_runtime_specs()
        if (include_batch or spec.pool == "INTERACTIVE")
        and (include_secondary or "secondary" not in spec.name)
    ]
    registered: list[dict[str, Any]] = []
    for spec in selected:
        target = await required_tool_result(
            client,
            "runtime_target_upsert",
            {
                "request": {
                    "idempotency_key": f"local-test-target-{run_id}-{spec.name}",
                    "name": spec.name,
                    "runtime_type": "JUPYTER",
                    "connection_config": {"endpoint": spec.endpoint},
                    "credential": spec.token,
                    "pool": spec.pool,
                    "max_concurrent_executions": spec.capacity,
                    "actor": {"type": "USER", "id": "local-test-operator"},
                }
            },
        )
        if target["state"]["status"] != "ACTIVE":
            raise RuntimeError(
                f"Runtime Target {spec.name} is not ACTIVE: {target['health']['last_error']}"
            )
        supported = set(target["runtime"]["supported_profiles"])
        if not {"basic", "ml"}.issubset(supported):
            raise RuntimeError(
                f"Runtime Target {spec.name} is missing basic/ml profiles: {sorted(supported)}"
            )
        registered.append(target)
    return registered


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def report_path(script_name: str, run_id: str) -> Path:
    root = Path(os.getenv("LOCAL_TEST_RESULTS_DIR", "test-results"))
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{script_name}-{run_id}.json"


def write_report(
    script_name: str, run_id: str, payload: dict[str, Any]
) -> Path:
    path = report_path(script_name, run_id)
    document = {
        "schema_version": "1.0",
        "script": script_name,
        "run_id": run_id,
        "recorded_at": utc_now_iso(),
        **payload,
    }
    path.write_text(
        json.dumps(
            document, ensure_ascii=False, indent=2, default=_json_default
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return str(value)
