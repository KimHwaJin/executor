"""HTTP and URL transport primitives for Jupyter Runtime Targets."""

from contextlib import AbstractAsyncContextManager
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit
from uuid import UUID

import httpx

from executor_service.domain.runtime import (
    RuntimeDriverError,
    RuntimeFileContent,
)
from executor_service.infrastructure._jupyter.file_content import (
    open_file_response,
)

_SAFE_STORAGE_REASONS = frozenset(
    {
        "Path must not be empty.",
        "Path must use forward slash separators.",
        "Path contains a control character.",
        "Path must not contain parent traversal.",
        "Path escapes the Jupyter root directory.",
        "Path is not a file.",
    }
)


def _storage_error_reason(response: httpx.Response) -> str:
    # Only known, constant validation messages may enter diagnostics.
    # Never copy arbitrary response bodies, paths, or credentials.
    if response.status_code != 422 or not response.request.url.path.endswith(
        "/executor/storage/files/metadata"
    ):
        return ""
    try:
        body = response.json()
    except ValueError:
        return ""
    if isinstance(body, dict):
        for key in ("message", "reason"):
            value = body.get(key)
            if isinstance(value, str) and value in _SAFE_STORAGE_REASONS:
                return f" reason={value}"
    return ""


class JupyterHttpTransport:
    def __init__(
        self,
        endpoint: str,
        token: str,
        request_timeout_seconds: float,
        storage_timeout_seconds: float,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.storage_timeout_seconds = storage_timeout_seconds
        self.client = httpx.AsyncClient(
            base_url=self.endpoint,
            headers={"Authorization": f"token {token}"},
            timeout=request_timeout_seconds,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        allowed_statuses: set[int] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        try:
            response = await self.client.request(method, path, **kwargs)
            if (
                allowed_statuses is None
                or response.status_code not in allowed_statuses
            ):
                response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            raise RuntimeDriverError(
                "Jupyter REST request failed: "
                f"method={method.upper()} path={path} "
                f"status={exc.response.status_code}."
                f"{_storage_error_reason(exc.response)}"
            ) from exc
        except httpx.RequestError as exc:
            raise RuntimeDriverError(
                "Jupyter REST request failed: "
                f"method={method.upper()} path={path} "
                f"transport={type(exc).__name__}."
            ) from exc

    def open_file(
        self, path: str, range_header: str | None
    ) -> AbstractAsyncContextManager[RuntimeFileContent]:
        return open_file_response(
            self.client, path, range_header, self.storage_timeout_seconds
        )

    def journal_uri(
        self, runtime_session_id: str, request_id: UUID, after: int
    ) -> str:
        parsed = urlsplit(self.endpoint)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        path = (
            f"{parsed.path.rstrip('/')}/executor/kernels/{runtime_session_id}"
            f"/executions/{request_id}/stream"
        )
        query = urlencode({"after": after})
        return urlunsplit((scheme, parsed.netloc, path, query, ""))
