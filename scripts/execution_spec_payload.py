"""Shared builders for Executor Execution API v2 smoke payloads."""

import hashlib
import json
import os
from typing import Any

from mcp.client.streamable_http import (
    create_mcp_http_client,
    streamable_http_client,
)

# Local-only key (admin, relay for any tenant) accepted by every local stack
# in this repository; never a deployed key. EXECUTOR_API_KEY overrides it.
LOCAL_API_KEY = "exe_local_dev_0123456789abcdef0123456789abcde"
LOCAL_AUTH_CLIENTS = json.dumps(
    [
        {
            "name": "local",
            "key_sha256": [hashlib.sha256(LOCAL_API_KEY.encode()).hexdigest()],
            "tenant": "dev",
            "relay": True,
            "allowed_tenants": ["*"],
            "admin": True,
        }
    ]
)
API_KEY_HEADERS = {"X-Api-Key": os.getenv("EXECUTOR_API_KEY") or LOCAL_API_KEY}


def inline_spec(
    steps: list[dict[str, Any]],
) -> dict[str, Any]:
    normalized_steps = []
    for sequence, step in enumerate(steps):
        values = dict(step)
        code = values.pop("code")
        lineage = {
            key: values.pop(key)
            for key in ("skill_name", "tool_name", "input_parameters")
            if key in values
        }
        normalized_steps.append(
            {
                "sequence": sequence,
                "payload": {
                    "type": "PYTHON_EXECUTE",
                    "source": {"type": "INLINE", "content": code},
                },
                **({"lineage": lineage} if lineage else {}),
                **values,
            }
        )
    return {"schema_version": "1.0", "steps": normalized_steps}


def execution_request(
    *,
    idempotency_key: str,
    operation_mode: str,
    trigger_type: str,
    runtime_profile: str,
    spec: dict[str, Any],
    operation_wait_timeout_seconds: int | None = None,
    operation_timeout_seconds: int | None = None,
    metadata: dict[str, Any] | None = None,
    operation_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lifecycle: dict[str, Any] = {"operation_mode": operation_mode}
    if operation_wait_timeout_seconds is not None:
        lifecycle["operation_wait_timeout_seconds"] = (
            operation_wait_timeout_seconds
        )
    operation: dict[str, Any] = {"spec": spec}
    if operation_timeout_seconds is not None:
        operation["operation_timeout_seconds"] = operation_timeout_seconds
    if operation_metadata:
        operation["metadata"] = operation_metadata
    return {
        "idempotency_key": idempotency_key,
        "lifecycle": lifecycle,
        "trigger": {"type": trigger_type},
        "runtime": {"type": "JUPYTER", "profile": runtime_profile},
        "operation": operation,
        "metadata": metadata or {},
    }


def identity_headers(
    user_id: str,
    project_id: str | None = None,
    session_id: str | None = None,
    tenant_id: str = "smoke",
) -> dict[str, str]:
    """The key and identity headers for a local Executor."""

    headers = {
        **API_KEY_HEADERS,
        "X-Tenant-Id": tenant_id,
        "X-User-Id": user_id,
    }
    if project_id is not None:
        headers["X-Project-Id"] = project_id
    if session_id is not None:
        headers["X-Session-Id"] = session_id
    return headers


SMOKE_HEADERS = identity_headers("smoke-user")
# No X-User-Id: reads see every Execution in the tenant.
SYSTEM_HEADERS = {**API_KEY_HEADERS, "X-Tenant-Id": "smoke"}


def executor_mcp(url: str, headers: dict[str, str] = SMOKE_HEADERS) -> Any:
    """An MCP transport that sends the identity headers on every request."""

    return streamable_http_client(
        url, http_client=create_mcp_http_client(headers=dict(headers))
    )
