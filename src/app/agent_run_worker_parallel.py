"""같은 프로세스에서 Agent Run을 여러 개 동시에 실행합니다.

claim은 기존 ``agent_run_worker.claim_one`` 입니다. ``FOR UPDATE SKIP LOCKED``가
루프끼리 같은 Run을 집지 않게 합니다. 기본 직렬 루프는 바꾸지 않습니다.

    AGENT_RUN_WORKER_PARALLEL=1 AGENT_RUN_WORKER_CONCURRENCY=4
"""

from __future__ import annotations

import asyncio
import os

from app.agent_run_worker import claim_one, execute_claimed
from config import settings


def concurrency() -> int:
    raw = os.environ.get("AGENT_RUN_WORKER_CONCURRENCY", "4")
    parsed = int(raw)
    if parsed < 1:
        raise ValueError("AGENT_RUN_WORKER_CONCURRENCY must be at least 1")
    return parsed


async def _loop(worker_id: str) -> None:
    interval = max(0.05, settings.agent_worker_poll_interval_seconds)
    while True:
        item = await claim_one(worker_id)
        if item is None:
            await asyncio.sleep(interval)
            continue
        await execute_claimed(item)


async def run_forever() -> None:
    """그래프 실행 슬롯을 concurrency 개수만큼 겹칩니다."""

    workers = concurrency()
    await asyncio.gather(*(_loop(str(index)) for index in range(workers)))
