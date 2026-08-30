"""In-process execution job dispatch and cancellation handoff."""

import asyncio
from collections.abc import Coroutine
from typing import Any
from uuid import UUID


class ExecutionJobDispatcher:
    """Owns local Tasks and enforces one active job per Execution."""

    def __init__(self) -> None:
        self._jobs: dict[UUID, asyncio.Task[None]] = {}
        self._idle = asyncio.Event()
        self._idle.set()
        self._accepting = False

    @property
    def accepting(self) -> bool:
        return self._accepting

    @property
    def active_job_count(self) -> int:
        return len(self._jobs)

    def set_accepting(self, accepting: bool) -> None:
        self._accepting = accepting

    def dispatch(
        self,
        execution_id: UUID,
        coroutine: Coroutine[Any, Any, None],
        *,
        replace: bool = False,
    ) -> None:
        current = self._jobs.get(execution_id)
        if not self._accepting and not replace:
            coroutine.close()
            return
        if current is not None and not current.done():
            if replace:
                if current.get_name() == f"cancellation-{execution_id}":
                    coroutine.close()
                    return
                current.cancel()
                task = asyncio.create_task(
                    self._handoff_to_cancellation(current, coroutine),
                    name=f"cancellation-{execution_id}",
                )
                self._track(execution_id, task)
            else:
                coroutine.close()
            return
        task = asyncio.create_task(
            coroutine,
            name=(
                f"cancellation-{execution_id}"
                if replace
                else f"execution-{execution_id}"
            ),
        )
        self._track(execution_id, task)

    async def wait_idle(self) -> None:
        await self._idle.wait()

    async def cancel_all(self) -> None:
        tasks = tuple(self._jobs.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _track(self, execution_id: UUID, task: asyncio.Task[None]) -> None:
        self._jobs[execution_id] = task
        self._idle.clear()
        task.add_done_callback(
            lambda done: self._remove_if_current(execution_id, done)
        )

    @staticmethod
    async def _handoff_to_cancellation(
        previous: asyncio.Task[None],
        cancellation: Coroutine[Any, Any, None],
    ) -> None:
        cancellation_started = False
        try:
            await asyncio.gather(previous, return_exceptions=True)
            cancellation_started = True
            await cancellation
        finally:
            if not cancellation_started:
                cancellation.close()

    def _remove_if_current(
        self, execution_id: UUID, task: asyncio.Task[None]
    ) -> None:
        if self._jobs.get(execution_id) is task:
            self._jobs.pop(execution_id, None)
            if not self._jobs:
                self._idle.set()
