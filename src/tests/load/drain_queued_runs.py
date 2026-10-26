"""부하 테스트 시작 전에 이전 실행이 남긴 큐를 비웁니다.

아직 집히지 않은 Run은 바로 canceled로 닫습니다.
이미 실행 중인 Run은 취소 요청만 남겨 워커가 슬롯을 놓게 합니다.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg


def database_url() -> str:
    if os.environ.get("DATABASE_URL"):
        url = os.environ["DATABASE_URL"]
    else:
        url = ""
        for line in Path(".env").read_text().splitlines():
            if line.startswith("DATABASE_URL="):
                url = line.split("=", 1)[1]
                break
    if not url:
        raise SystemExit("DATABASE_URL is required")
    return url.replace("postgresql+asyncpg", "postgresql", 1)


async def drain() -> None:
    conn = await asyncpg.connect(database_url())
    try:
        async with conn.transaction():
            pending_tasks = await conn.fetch(
                """
                UPDATE agent_runs
                SET status = 'canceled',
                    completed_at = now(),
                    cancel_requested_at = coalesce(cancel_requested_at, now()),
                    cancel_reason = 'drained before load test',
                    failure = jsonb_build_object('message', 'Drained before load test.'),
                    updated_at = now()
                WHERE status = 'pending'
                RETURNING task_id
                """
            )
            if pending_tasks:
                await conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'canceled',
                        completed_at = now(),
                        cancel_requested_at = coalesce(cancel_requested_at, now()),
                        lock_owner = NULL,
                        lock_token = NULL,
                        heartbeat_at = NULL,
                        lease_expires_at = NULL,
                        failure_reason = 'Drained before load test.',
                        updated_at = now()
                    WHERE task_id = ANY($1::uuid[])
                      AND status IN ('pending', 'running', 'waiting_input')
                    """,
                    [row["task_id"] for row in pending_tasks if row["task_id"] is not None],
                )
            running = await conn.fetch(
                """
                UPDATE agent_runs
                SET cancel_requested_at = now(),
                    cancel_reason = 'drained before load test',
                    updated_at = now()
                WHERE status = 'running'
                  AND cancel_requested_at IS NULL
                RETURNING task_id
                """
            )
            if running:
                await conn.execute(
                    """
                    UPDATE tasks
                    SET cancel_requested_at = coalesce(cancel_requested_at, now()),
                        updated_at = now()
                    WHERE task_id = ANY($1::uuid[])
                      AND status IN ('pending', 'running', 'waiting_input')
                    """,
                    [row["task_id"] for row in running if row["task_id"] is not None],
                )
        print(
            f"drained_pending={len(pending_tasks)} "
            f"cancel_requested_running={len(running)}"
        )
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(drain())
