# 본사에서 테스트할 때 사용하는 router.py
import asyncio
import os
from contextlib import asynccontextmanager, suppress

from fastapi import APIRouter
from app.api.v1.routes import (
    jupyter_servers,
    messages,
    projects,
    redis,
    runs,
    sessions,
    tasks,
    users,
    workflows,
    workflow_run,
)
from config import settings
from app.task_lock_reconciler import run_forever as run_task_lock_reconciler
from app.agent_run_worker import run_forever as run_agent_worker


def _enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


@asynccontextmanager
async def router_lifespan(_app):
    """Gaia가 이 Router만 include해도 E03 stale-lock 복구를 함께 실행합니다."""

    if settings.task_reconciler_enabled:
        if _enabled("TASK_RECONCILER_PARALLEL"):
            from app.task_lock_reconciler_parallel import run_forever as run_reconciler
        else:
            run_reconciler = run_task_lock_reconciler
        reconciler_task = asyncio.create_task(
            run_reconciler(), name="task-lock-reconciler"
        )
    else:
        reconciler_task = None
    if settings.agent_worker_enabled:
        if _enabled("AGENT_RUN_WORKER_PARALLEL"):
            from app.agent_run_worker_parallel import run_forever as run_worker
        else:
            run_worker = run_agent_worker
        agent_worker_task = asyncio.create_task(
            run_worker(), name="agent-run-worker"
        )
    else:
        agent_worker_task = None
    try:
        yield
    finally:
        if reconciler_task is not None:
            reconciler_task.cancel()
            with suppress(asyncio.CancelledError):
                await reconciler_task
        if agent_worker_task is not None:
            agent_worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await agent_worker_task
# Reconciler를 main이 아니라 등록 대상 Router의 생애주기에 귀속합니다.
api_router = APIRouter(lifespan=router_lifespan)
api_router.include_router(users.router)
api_router.include_router(projects.router)
api_router.include_router(sessions.router)
api_router.include_router(messages.router)
api_router.include_router(runs.router)
api_router.include_router(workflows.router)
api_router.include_router(jupyter_servers.router)
api_router.include_router(redis.router)
api_router.include_router(tasks.router)
api_router.include_router(workflow_run.router)
