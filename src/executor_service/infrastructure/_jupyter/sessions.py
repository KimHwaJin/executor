"""Jupyter kernel session lifecycle."""

import asyncio

from executor_service.domain.enums import RuntimeAbortStatus
from executor_service.domain.runtime import (
    RuntimeAbortResult,
    RuntimeDriverError,
)
from executor_service.infrastructure._jupyter.transport import (
    JupyterHttpTransport,
)


class JupyterSessionClient:
    def __init__(self, transport: JupyterHttpTransport) -> None:
        self._transport = transport

    async def start(self, profile: str, working_directory: str) -> str:
        response = await self._transport.request(
            "POST",
            "/api/kernels",
            json={"name": profile, "path": working_directory},
        )
        return str(response.json()["id"])

    async def interrupt(self, session_id: str) -> None:
        await self._transport.request(
            "POST",
            f"/api/kernels/{session_id}/interrupt",
            allowed_statuses={204, 404},
        )

    async def abort(
        self, session_id: str, timeout_seconds: float
    ) -> RuntimeAbortResult:
        try:
            interrupt = await self._transport.request(
                "POST",
                f"/api/kernels/{session_id}/interrupt",
                allowed_statuses={204, 404},
            )
            if interrupt.status_code == 404:
                return RuntimeAbortResult(
                    RuntimeAbortStatus.SESSION_MISSING,
                    "Runtime session disappeared before interruption.",
                )
            async with asyncio.timeout(timeout_seconds):
                while True:
                    response = await self._transport.request(
                        "GET",
                        f"/api/kernels/{session_id}",
                        allowed_statuses={200, 404},
                    )
                    if response.status_code == 404:
                        return RuntimeAbortResult(
                            RuntimeAbortStatus.SESSION_MISSING,
                            "Runtime session disappeared while confirming abort.",
                        )
                    try:
                        execution_state = response.json()["execution_state"]
                    except (KeyError, TypeError, ValueError) as exc:
                        raise RuntimeDriverError(
                            "Jupyter kernel status response is invalid."
                        ) from exc
                    if execution_state == "idle":
                        return RuntimeAbortResult(
                            RuntimeAbortStatus.IDLE_CONFIRMED
                        )
                    await asyncio.sleep(min(0.25, timeout_seconds / 10))
        except TimeoutError:
            return RuntimeAbortResult(
                RuntimeAbortStatus.FAILED,
                "Runtime did not confirm an idle session before the abort "
                "deadline.",
            )
        except RuntimeDriverError as exc:
            return RuntimeAbortResult(
                RuntimeAbortStatus.FAILED,
                str(exc)[:2000],
            )

    async def delete(self, session_id: str) -> None:
        await self._transport.request(
            "DELETE",
            f"/api/kernels/{session_id}",
            allowed_statuses={204, 404},
        )

    async def exists(self, session_id: str) -> bool:
        response = await self._transport.request(
            "GET",
            f"/api/kernels/{session_id}",
            allowed_statuses={200, 404},
        )
        return response.status_code == 200
