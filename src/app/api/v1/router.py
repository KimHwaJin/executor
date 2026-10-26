import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import APIRouter

from app.api.v1.routes import (
    chat_completion,
    jupyter_servers,
    messages,
    projects,
    redis,
    runs,
    sessions,
    tasks,
    users,
    workflow_catalog,
    workflow_run,
)
from config import settings
from app.task_lock_reconciler import run_forever as run_task_lock_reconciler
from app.agent_run_worker import run_forever as run_agent_worker


@asynccontextmanager
async def router_lifespan(_app):
    """Gaia가 이 Router만 include해도 E03 stale-lock 복구를 함께 실행합니다."""

    reconciler_task = (
        asyncio.create_task(
            run_task_lock_reconciler(), name="task-lock-reconciler"
        )
        if settings.task_reconciler_enabled
        else None
    )
    agent_worker_task = (
        asyncio.create_task(run_agent_worker(), name="agent-run-worker")
        if settings.agent_worker_enabled
        else None
    )
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
api_router.include_router(chat_completion.router)
api_router.include_router(messages.router)
api_router.include_router(runs.router)
api_router.include_router(workflow_catalog.router)
api_router.include_router(jupyter_servers.router)
api_router.include_router(redis.router)
api_router.include_router(tasks.router)

api_router.include_router(workflow_run.router)
