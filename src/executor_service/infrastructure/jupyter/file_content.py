"""Open a Runtime file response before exposing its metadata to HTTP."""

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from executor_service.domain.runtime import (
    RuntimeByteRange,
    RuntimeDriverError,
    RuntimeFileContent,
    RuntimeFileRangeError,
    RuntimeFileUnavailableError,
)


def _metadata(response: httpx.Response) -> tuple[RuntimeByteRange, str]:
    try:
        length_text = response.headers["Content-Length"]
        if not re.fullmatch(r"[0-9]+", length_text):
            raise ValueError
        length = int(length_text)
        if response.status_code == 206:
            match = re.fullmatch(
                r"bytes ([0-9]+)-([0-9]+)/([0-9]+)",
                response.headers["Content-Range"],
            )
            if match is None:
                raise ValueError
            start, end, size = map(int, match.groups())
            if not (0 <= start <= end < size) or end - start + 1 != length:
                raise ValueError
            byte_range = RuntimeByteRange(start, end, size, True)
        elif response.status_code == 200:
            byte_range = RuntimeByteRange(0, length - 1, length, False)
        else:
            raise ValueError
        checksum = response.headers["X-Checksum-SHA256"]
        if not re.fullmatch(r"[a-f0-9]{64}", checksum):
            raise ValueError
        if response.headers.get("ETag") != f'"{checksum}"':
            raise ValueError
        if response.headers.get("Content-Encoding", "identity") != "identity":
            raise ValueError
        return byte_range, checksum
    except (KeyError, ValueError) as exc:
        raise RuntimeDriverError(
            "Jupyter file download metadata is invalid; update the Runtime extension."
        ) from exc


async def _body(response: httpx.Response, length: int) -> AsyncIterator[bytes]:
    received = 0
    try:
        async for chunk in response.aiter_bytes():
            received += len(chunk)
            if received > length:
                raise RuntimeDriverError(
                    "Jupyter file response exceeded its length."
                )
            if chunk:
                yield chunk
    except httpx.RequestError as exc:
        raise RuntimeDriverError(
            "Jupyter file stream interrupted: "
            f"transport={type(exc).__name__}, received={received}, expected={length}."
        ) from exc
    if received != length:
        raise RuntimeDriverError(
            "Jupyter file response ended before its length."
        )


@asynccontextmanager
async def open_file_response(
    client: httpx.AsyncClient,
    path: str,
    range_header: str | None,
    timeout_seconds: float,
) -> AsyncIterator[RuntimeFileContent]:
    headers = {"Accept-Encoding": "identity"}
    if range_header is not None:
        headers["Range"] = range_header
    try:
        async with client.stream(
            "GET",
            "/executor/storage/files/content",
            params={"path": path},
            headers=headers,
            timeout=timeout_seconds,
        ) as response:
            if response.status_code == 416:
                match = re.fullmatch(
                    r"bytes \*/([0-9]+)",
                    response.headers.get("Content-Range", ""),
                )
                if match is None:
                    raise RuntimeDriverError(
                        "Jupyter file range response is invalid."
                    )
                raise RuntimeFileRangeError(int(match[1]))
            if response.status_code in {404, 409}:
                raise RuntimeFileUnavailableError(
                    "Runtime file was not found."
                    if response.status_code == 404
                    else "Runtime file changed during download setup; retry after saving."
                )
            response.raise_for_status()
            byte_range, checksum = _metadata(response)
            yield RuntimeFileContent(
                byte_range, checksum, _body(response, byte_range.length)
            )
    except httpx.HTTPStatusError as exc:
        raise RuntimeDriverError(
            f"Jupyter file content request failed: status={exc.response.status_code}."
        ) from exc
    except httpx.RequestError as exc:
        raise RuntimeDriverError(
            "Jupyter file content request failed: "
            f"transport={type(exc).__name__}."
        ) from exc
