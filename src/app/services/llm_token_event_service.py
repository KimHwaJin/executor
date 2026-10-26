"""LangChain streaming callback을 durable Task SSE token event로 변환합니다."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler

from config import settings
from app.core.database import get_session_factory
from app.services.task_event_service import TaskEventService


class LLMTokenEventBuffer(AsyncCallbackHandler):
    """Callback 경로에서는 queue만 쓰고, DB 저장은 별도 coroutine에서 수행합니다."""

    def __init__(self, *, task_id: UUID, run_id: UUID) -> None:
        self.task_id = task_id
        self.run_id = run_id
        self.queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()
        self.offsets: dict[str, int] = defaultdict(int)
        self.consumer: asyncio.Task | None = None

    async def on_llm_new_token(self, token: str, *, run_id, **_kwargs) -> None:
        if token:
            await self.queue.put((str(run_id), token))

    async def _append(self, llm_run_id: str, delta: str) -> None:
        start = self.offsets[llm_run_id]
        end = start + len(delta)
        async with get_session_factory()() as db:
            await TaskEventService.append(
                db,
                task_id=self.task_id,
                run_id=self.run_id,
                event_type="llm.token.delta",
                payload={
                    "llm_run_id": llm_run_id,
                    "delta": delta,
                    "offset_start": start,
                    "offset_end": end,
                },
            )
        self.offsets[llm_run_id] = end

    async def _consume(self) -> None:
        pending: dict[str, list[str]] = defaultdict(list)
        interval = max(0.05, settings.llm_token_flush_interval_seconds)
        while True:
            try:
                item = await asyncio.wait_for(self.queue.get(), timeout=interval)
            except asyncio.TimeoutError:
                item = ("", "")
            if item is None:
                for llm_run_id, chunks in pending.items():
                    if chunks:
                        await self._append(llm_run_id, "".join(chunks))
                return
            llm_run_id, token = item
            if llm_run_id:
                pending[llm_run_id].append(token)
            size = sum(len(part) for chunks in pending.values() for part in chunks)
            if size >= settings.llm_token_flush_characters or not llm_run_id:
                for key, chunks in list(pending.items()):
                    if chunks:
                        await self._append(key, "".join(chunks))
                        pending[key].clear()

    def start(self) -> None:
        self.consumer = asyncio.create_task(
            self._consume(), name=f"llm-token-events:{self.run_id}"
        )

    async def close(self) -> None:
        await self.queue.put(None)
        if self.consumer is not None:
            await self.consumer

