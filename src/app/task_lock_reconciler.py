"""E03 stale Task lock reconciler 실행 진입점.

기존 API 구조를 바꾸지 않기 위해 독립 프로세스로 실행합니다.
운영 환경에서는 API와 별도 Deployment/CronJob으로 한 개 이상 실행할 수 있으며,
DB의 ``FOR UPDATE SKIP LOCKED``가 여러 reconciler의 중복 처리를 막습니다.
"""

from __future__ import annotations

import argparse
import asyncio

from config import settings
from app.core.database import get_session_factory
from app.services.task_service import TaskService


async def reconcile_once() -> int:
    """E03-T06: 만료된 활성 Task를 한 batch 복구하고 처리 개수를 반환합니다.

    취소 요청 후 Worker가 사라진 Task도 lease 만료 시 timeout으로 종결되어
    Session lock이 영구적으로 남지 않습니다.
    """

    async with get_session_factory()() as db:
        recovered = await TaskService.reconcile_stale(db)
        return len(recovered)


async def run_forever() -> None:
    """E03-T06: 설정된 주기로 stale lease를 계속 복구합니다."""

    while True:
        await reconcile_once()
        await asyncio.sleep(settings.task_reconcile_interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Recover stale E03 Task locks")
    parser.add_argument(
        "--once",
        action="store_true",
        help="한 batch만 복구하고 종료합니다.",
    )
    args = parser.parse_args()
    if args.once:
        recovered_count = asyncio.run(reconcile_once())
        print(f"recovered_stale_tasks={recovered_count}")
        return
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
