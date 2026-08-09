"""Shared builders for Executor MCP ExecutionSpec v1 smoke payloads."""

from typing import Any


def inline_source(
    execution_plan_id: str,
    steps: list[dict[str, Any]],
) -> dict[str, Any]:
    normalized_steps = []
    for sequence, step in enumerate(steps):
        normalized_steps.append(
            {
                "sequence": sequence,
                "plan_step_id": f"{execution_plan_id}-step-{sequence}",
                **step,
            }
        )
    return {
        "type": "INLINE",
        "spec": {
            "schema_version": "1.0",
            "execution_plan_id": execution_plan_id,
            "steps": normalized_steps,
        },
    }
