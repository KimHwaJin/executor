from __future__ import annotations

from collections.abc import Mapping

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


def error_code_for_status(status_code: int) -> str:
    return {
        400: "COMMON-BAD-REQUEST-001",
        401: "AUTH-UNAUTHORIZED-001",
        403: "AUTH-FORBIDDEN-001",
        404: "COMMON-NOT-FOUND-001",
        409: "COMMON-CONFLICT-001",
        412: "COMMON-PRECONDITION-FAILED-001",
        422: "COMMON-VALIDATION-001",
        429: "COMMON-RATE-LIMITED-001",
        503: "RUNTIME-UNAVAILABLE-001",
    }.get(status_code, "COMMON-INTERNAL-ERROR-001")


def problem_response(
    request: Request,
    *,
    status_code: int,
    title: str,
    detail: str,
    error_code: str | None = None,
    errors: list[dict[str, str]] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, object] = {
        "type": f"https://api.example.com/problems/{error_code or error_code_for_status(status_code)}",
        "title": title,
        "status": status_code,
        "detail": detail,
        "instance": request.url.path,
        "error_code": error_code or error_code_for_status(status_code),
        "request_id": request.state.request_id,
    }
    if errors:
        body["errors"] = errors
    return JSONResponse(
        status_code=status_code,
        content=body,
        media_type="application/problem+json",
        headers=headers,
    )


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    titles = {
        400: "Bad request",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Resource not found",
        409: "Request conflict",
        412: "Precondition failed",
        422: "Request validation failed",
    }
    return problem_response(
        request,
        status_code=exc.status_code,
        title=titles.get(exc.status_code, "Request failed"),
        detail=str(exc.detail),
        headers=exc.headers,
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    errors = [
        {
            "field": ".".join(str(part) for part in item["loc"] if part != "body"),
            "reason": item["type"],
            "message": item["msg"],
        }
        for item in exc.errors()
    ]
    return problem_response(
        request,
        status_code=422,
        title="Request validation failed",
        detail="One or more request fields are invalid.",
        errors=errors,
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Avoid leaking database or runtime details in 500 responses."""
    return problem_response(
        request,
        status_code=500,
        title="Internal server error",
        detail="An unexpected server error occurred.",
    )

