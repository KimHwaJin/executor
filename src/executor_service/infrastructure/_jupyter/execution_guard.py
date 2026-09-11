"""Bound execution waits to the lifetime of the original kernel process."""

import asyncio
from collections.abc import Awaitable, Callable
from urllib.parse import quote

from executor_service.domain.runtime import (
    RuntimeDriverError,
    RuntimeExecutionError,
    RuntimeSessionLostError,
)
from executor_service.infrastructure._jupyter.transport import (
    JupyterHttpTransport,
)


class KernelExecutionGuard:
    def __init__(
        self,
        transport: JupyterHttpTransport,
        *,
        poll_seconds: float = 10,
        probe_timeout_seconds: float = 5,
        failure_threshold: int = 3,
    ) -> None:
        self._transport = transport
        self._poll_seconds = poll_seconds
        self._probe_timeout_seconds = probe_timeout_seconds
        self._failure_threshold = failure_threshold

    async def _instance(self, kernel_id: str) -> str:
        try:
            async with asyncio.timeout(self._probe_timeout_seconds):
                response = await self._transport.request(
                    "GET",
                    f"/executor/kernels/{quote(kernel_id, safe='')}/execution-state",
                    timeout=self._probe_timeout_seconds,
                )
                data = response.json()
        except TimeoutError as exc:
            raise RuntimeDriverError(
                "Jupyter kernel continuity probe timed out."
            ) from exc
        except ValueError as exc:
            raise RuntimeDriverError(
                "Jupyter kernel continuity response is invalid."
            ) from exc
        if (
            not isinstance(data, dict)
            or data.get("kernel_id") != kernel_id
            or type(data.get("alive")) is not bool
        ):
            raise RuntimeDriverError(
                "Jupyter kernel continuity response is invalid."
            )
        if not data["alive"]:
            raise RuntimeSessionLostError(
                "Jupyter kernel process is unavailable or shutting down; "
                "the submitted execution cannot continue. Cause is unknown."
            )
        instance = data.get("instance_id")
        if not isinstance(instance, str) or not instance:
            raise RuntimeDriverError(
                "Jupyter kernel continuity response has no process identity."
            )
        return instance

    async def _watch(self, kernel_id: str, original: str) -> None:
        failures = 0
        while True:
            await asyncio.sleep(self._poll_seconds)
            try:
                current = await self._instance(kernel_id)
            except RuntimeSessionLostError:
                raise
            except RuntimeDriverError as exc:
                failures += 1
                if failures >= self._failure_threshold:
                    raise RuntimeDriverError(
                        "Jupyter kernel continuity could not be verified after "
                        f"{failures} consecutive probes; execution monitoring "
                        "was stopped. Kernel termination is not confirmed."
                    ) from exc
                continue
            failures = 0
            if current != original:
                raise RuntimeSessionLostError(
                    "Jupyter kernel process changed during execution; "
                    "the original execution state was lost. Cause is unknown."
                )

    async def run[T](
        self, kernel_id: str, execute: Callable[[], Awaitable[T]]
    ) -> T:
        # Fail before sending code if an old/misconfigured extension cannot
        # provide continuity. Never silently disable execution-loss detection.
        original = await self._instance(kernel_id)

        async def invoke() -> T:
            return await execute()

        execution = asyncio.create_task(invoke(), name="jupyter-execute")
        watcher = asyncio.create_task(
            self._watch(kernel_id, original), name="jupyter-continuity"
        )
        try:
            done, _ = await asyncio.wait(
                {execution, watcher}, return_when=asyncio.FIRST_COMPLETED
            )
            if watcher in done:
                await watcher
            try:
                result = await execution
            except RuntimeExecutionError:
                # A manual restart first interrupts the old kernel. Its
                # KeyboardInterrupt reply is not permission to reuse that
                # kernel: confirm continuity before returning a code error.
                await self._confirm(kernel_id, original)
                raise
            await self._confirm(kernel_id, original)
            return result
        finally:
            for task in (execution, watcher):
                if not task.done():
                    task.cancel()
            await asyncio.gather(execution, watcher, return_exceptions=True)

    async def _confirm(self, kernel_id: str, original: str) -> None:
        for failure in range(self._failure_threshold):
            try:
                current = await self._instance(kernel_id)
            except RuntimeSessionLostError:
                raise
            except RuntimeDriverError:
                if failure + 1 == self._failure_threshold:
                    raise
                await asyncio.sleep(self._poll_seconds)
                continue
            if current != original:
                raise RuntimeSessionLostError(
                    "Jupyter kernel process changed before execution completion "
                    "could be confirmed. The original runtime state was lost. "
                    "Cause is unknown."
                )
            return
