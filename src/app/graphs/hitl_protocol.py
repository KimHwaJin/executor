"""Standard Agent Chat HITL request and response transport helpers."""

from __future__ import annotations

import json
from typing import Any

from langgraph.types import interrupt


def build_hitl_request(
    *,
    action_name: str,
    description: str,
    args: dict[str, Any],
    allowed_decisions: list[str] | None = None,
    args_schema: dict[str, Any] | None = None,
    payload_version: int = 1,
) -> dict[str, Any]:
    """Wrap domain UI metadata in Agent Chat's current HITL envelope."""
    if type(payload_version) is not int or payload_version not in (1, 2):
        raise ValueError("Unsupported HITL payload_version")
    review_config = {
        "action_name": action_name,
        "allowed_decisions": allowed_decisions or ["respond"],
    }
    if args_schema is not None:
        review_config["args_schema"] = args_schema
    request = {
        "action_requests": [
            {
                "name": action_name,
                "args": args,
                "description": description,
            }
        ],
        "review_configs": [
            review_config
        ],
    }
    # Keep the legacy interrupt shape unchanged for existing callers/checkpoints.
    if payload_version != 1:
        request["payload_version"] = payload_version
    return request


def hitl_request_args(request: Any) -> dict[str, Any]:
    """Return the domain-specific args carried by one HITL request."""
    if isinstance(request, list) and len(request) == 1:
        item = request[0]
        if isinstance(item, dict):
            action_request = item.get("action_request")
            if isinstance(action_request, dict) and isinstance(
                action_request.get("args"), dict
            ):
                return action_request["args"]

    if not isinstance(request, dict):
        raise ValueError("HITL request must be an object or one-item list")
    action_requests = request.get("action_requests")
    if not isinstance(action_requests, list) or len(action_requests) != 1:
        raise ValueError("HITL request must contain exactly one action")
    action = action_requests[0]
    if not isinstance(action, dict) or not isinstance(action.get("args"), dict):
        raise ValueError("HITL action requires object args")
    return action["args"]


def unwrap_hitl_response(
    response: Any,
    *,
    default_args: dict[str, Any] | None = None,
) -> Any:
    """Accept Agent Chat HITL responses and legacy direct resume values."""
    # 외부 연동에서 전달한 resume envelope를 벗긴다.
    if isinstance(response, dict) and set(response) == {"resume"}:
        response = response["resume"]    
    if isinstance(response, list):
        if len(response) != 1 or not isinstance(response[0], dict):
            raise ValueError("HumanInterrupt response must contain one item")
        human_response = response[0]
        if human_response.get("type") != "response":
            raise ValueError("HumanInterrupt must use a response action")
        if "args" not in human_response:
            raise ValueError("HumanInterrupt response requires args")
        return _parse_response_value(human_response["args"])

    if not isinstance(response, dict) or "decisions" not in response:
        return response

    decisions = response.get("decisions")
    if not isinstance(decisions, list) or len(decisions) != 1:
        raise ValueError("HITL response must contain exactly one decision")

    decision = decisions[0]
    if not isinstance(decision, dict):
        raise ValueError("HITL decision must be an object")

    decision_type = decision.get("type")
    if decision_type == "approve":
        if default_args is None:
            raise ValueError("HITL approve decision requires default args")
        return default_args
    if decision_type == "edit":
        edited_action = decision.get("edited_action")
        if not isinstance(edited_action, dict) or not isinstance(
            edited_action.get("args"), dict
        ):
            raise ValueError("HITL edit decision requires edited_action.args")
        return edited_action["args"]
    if decision_type == "reject":
        message = decision.get("message")
        if not isinstance(message, str) or not message.strip():
            message = "Workflow 후보를 거절했습니다."
        return {"approved": False, "feedback": message.strip()}
    if decision_type != "respond":
        raise ValueError(f"Unsupported HITL decision: {decision_type}")
    if "message" not in decision:
        raise ValueError("HITL respond decision requires message")

    return _parse_response_value(decision["message"])


def _parse_response_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return ""
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return stripped


def request_human_input(
    *,
    action_name: str,
    description: str,
    args: dict[str, Any],
    allowed_decisions: list[str] | None = None,
    args_schema: dict[str, Any] | None = None,
    payload_version: int = 1,
) -> Any:
    """Interrupt using the standard envelope and return the domain value."""
    response = interrupt(
        build_hitl_request(
            action_name=action_name,
            description=description,
            args=args,
            allowed_decisions=allowed_decisions,
            args_schema=args_schema,
            payload_version=payload_version,
        )
    )
    return unwrap_hitl_response(response, default_args=args)


__all__ = [
    "build_hitl_request",
    "hitl_request_args",
    "request_human_input",
    "unwrap_hitl_response",
]
