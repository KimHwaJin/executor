"""The one JSON shape every error response uses."""

from collections.abc import Iterable
from typing import Any

from executor_service.domain.errors import DomainError, ErrorIssue


def issue_json(issue: ErrorIssue) -> dict[str, Any]:
    body: dict[str, Any] = {
        "location": list(issue.location),
        "message": issue.message,
        "type": issue.type,
    }
    if issue.context:
        body["context"] = dict(issue.context)
    return body


def error_content(
    code: str, message: str, details: Iterable[ErrorIssue] = ()
) -> dict[str, Any]:
    """``{"error": {code, message, details}}``; details is [] when empty."""
    return {
        "error": {
            "code": code,
            "message": message,
            "details": [issue_json(issue) for issue in details],
        }
    }


def domain_error_content(exc: DomainError) -> dict[str, Any]:
    return error_content(exc.code, str(exc), exc.details)
