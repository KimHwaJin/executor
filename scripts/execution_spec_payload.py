"""Shared builders for Executor Execution API v2 smoke payloads."""

from typing import Any


def inline_source(
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
                "payload": {"type": "CODE", "content": code},
                **({"lineage": lineage} if lineage else {}),
                **values,
            }
        )
    return {
        "type": "INLINE",
        "spec": {
            "schema_version": "1.0",
            "steps": normalized_steps,
        },
    }


def execution_request(
    *,
    idempotency_key: str,
    operation_mode: str,
    trigger_type: str,
    actor: dict[str, str],
    runtime_profile: str,
    source: dict[str, Any],
    context: dict[str, Any],
    operation_wait_timeout_seconds: int | None = None,
    operation_timeout_seconds: int | None = None,
    metadata: dict[str, Any] | None = None,
    operation_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lifecycle: dict[str, Any] = {"operation_mode": operation_mode}
    if operation_wait_timeout_seconds is not None:
        lifecycle["operation_wait_timeout_seconds"] = operation_wait_timeout_seconds
    operation: dict[str, Any] = {"source": source}
    if operation_timeout_seconds is not None:
        operation["operation_timeout_seconds"] = operation_timeout_seconds
    if operation_metadata:
        operation["metadata"] = operation_metadata
    return {
        "idempotency_key": idempotency_key,
        "lifecycle": lifecycle,
        "trigger": {"type": trigger_type, "actor": actor},
        "runtime": {"type": "JUPYTER", "profile": runtime_profile},
        "context": context,
        "operation": operation,
        "metadata": metadata or {},
    }
