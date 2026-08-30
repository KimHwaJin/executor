"""HTTP and URL transport primitives for Jupyter Runtime Targets."""

from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

from executor_service.domain.runtime import RuntimeDriverError


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
            ) from exc
        except httpx.RequestError as exc:
            raise RuntimeDriverError(
                "Jupyter REST request failed: "
                f"method={method.upper()} path={path} "
                f"transport={type(exc).__name__}."
            ) from exc

    async def stream_file(
        self, path: str, start: int, end: int
    ) -> AsyncIterator[bytes]:
        try:
            async with self.client.stream(
                "GET",
                "/executor/storage/files/content",
                params={"path": path, "start": start, "end": end},
                timeout=self.storage_timeout_seconds,
            ) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    if chunk:
                        yield chunk
        except httpx.HTTPStatusError as exc:
            raise RuntimeDriverError(
                "Jupyter file content request failed: "
                f"status={exc.response.status_code}."
            ) from exc
        except httpx.RequestError as exc:
            raise RuntimeDriverError(
                "Jupyter file content request failed: "
                f"transport={type(exc).__name__}."
            ) from exc

    def channels_uri(self, runtime_session_id: str, session_id: str) -> str:
        parsed = urlsplit(self.endpoint)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        base_path = parsed.path.rstrip("/")
        path = f"{base_path}/api/kernels/{runtime_session_id}/channels"
        query = urlencode({"session_id": session_id})
        return urlunsplit((scheme, parsed.netloc, path, query, ""))
