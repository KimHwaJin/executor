"""stale Task lock reconciler를 여러 루프로 돌립니다.

각 루프는 기존 ``reconcile_once``를 호출합니다. 조회는 ``SKIP LOCKED``라
같은 만료 Task를 두 루프가 같이 고치지 않습니다.

    TASK_RECONCILER_PARALLEL=1 TASK_RECONCILER_CONCURRENCY=4
"""

from __future__ import annotations

import asyncio
import os

from app.task_lock_reconciler import reconcile_once
from config import settings


def concurrency() -> int:
    raw = os.environ.get("TASK_RECONCILER_CONCURRENCY", "4")
    parsed = int(raw)
    if parsed < 1:
        raise ValueError("TASK_RECONCILER_CONCURRENCY must be at least 1")
    return parsed


async def _loop(slot: int, workers: int) -> None:
    interval = max(0.05, settings.task_reconcile_interval_seconds)
    await asyncio.sleep(interval * slot / workers)
    while True:
        await reconcile_once()
        await asyncio.sleep(interval)


async def run_forever() -> None:
    """복구 batch를 concurrency 개수만큼 겹칩니다."""

    workers = concurrency()
    await asyncio.gather(*(_loop(index, workers) for index in range(workers)))
