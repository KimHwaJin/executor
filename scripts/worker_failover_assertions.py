"""Shared assertions for process, Docker, and Kubernetes Worker-loss tests."""

from typing import Any


class FailoverValidationError(RuntimeError):
    """Raised when a Worker-loss recovery invariant is not satisfied."""


def validate_event_history(events: list[dict[str, Any]]) -> None:
    sequences = [event["event_sequence"] for event in events]
    if sequences != list(range(1, len(events) + 1)):
        raise FailoverValidationError(
            f"Event sequence is not contiguous: {sequences}"
        )
    event_ids = [event["event_id"] for event in events]
    if len(event_ids) != len(set(event_ids)):
        raise FailoverValidationError(
            "Duplicate durable event_id values were found."
        )
    completed = [
        event["payload"]["status"]
        for event in events
        if event["event_type"] == "execution.completed"
    ]
    if completed != ["FAILED", "SUCCEEDED"]:
        raise FailoverValidationError(
            "Expected one failed and one successful completion cycle; "
            f"found {completed}."
        )


def validate_attempt_history(
    attempts: list[dict[str, Any]],
    *,
    initial_session_id: str,
) -> None:
    if len(attempts) != 2:
        raise FailoverValidationError(
            f"Expected exactly two Attempts: {attempts}"
        )
    first, second = attempts
    if first["lease"]["owner"] is not None:
        raise FailoverValidationError(
            "The failed Attempt lease owner was not released."
        )
    if first["state"]["status"] != "FAILED":
        raise FailoverValidationError(
            "The first Attempt was not fenced as FAILED."
        )
    if first["failure"]["type"] != "LEASE_EXPIRED":
        raise FailoverValidationError(
            "The first Attempt was not LEASE_EXPIRED."
        )
    if first["recovery"]["runtime_session_cleanup_status"] != "SUCCEEDED":
        raise FailoverValidationError(
            "The abandoned Runtime session was not cleaned up."
        )
    if second["state"]["status"] != "SUCCEEDED":
        raise FailoverValidationError("The retry Attempt did not succeed.")
    if second["runtime"]["session_id"] == initial_session_id:
        raise FailoverValidationError(
            "FROM_START retry reused the abandoned session."
        )
