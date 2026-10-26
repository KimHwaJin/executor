from scripts.docker_entrypoint import bootstrap_db
import sys
from pathlib import Path
import asyncio

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    def _selector_loop_factory(use_subprocess: bool = False):
        return asyncio.SelectorEventLoop
    import uvicorn.loops.asyncio as uv_asyncio
    import uvicorn.loops.auto as uv_auto
    uv_asyncio.asyncio_loop_factory = _selector_loop_factory
    uv_auto.auto_loop_factory = _selector_loop_factory

src_path = Path(__file__).parent / "src"
sys.path.insert(0, str(src_path))
print(f"System Path: {src_path}")

from src.gaia.core import GaiaService 

from src.app.task_lock_reconciler import run_forever as run_task_lock_reconicler
from src.app.agent_run_worker import run_forever as run_agent_worker
from src.config import settings
from src.app.core.store_container import container

###
from contextlib import asynccontextmanager, suppress
import uvicorn

@asynccontextmanager
async def custom_lifespan(app):
    """Gaia가 이 Router만 include 해도 E03 stale-lock 복구를 함께 실행합니다."""
    reconciler_task = (
        asyncio.create_task(run_task_lock_reconciler(), name="task-lock-reconciler")
        if settings.task_reconciler_enabled
        else None
    )
    agent_worker_task = (
        asyncio.create_task(run_agent_worker(), name="agent-run-worker")
        if settings.agent_worker_enabled
        else None
    )
    try:
        print("custom agent lifespan on")
        container.startup()
        yield
        container.shutdown()
    finally:
        if reconciler_task is not None:
            reconciler_task.cancel()
            with suppress(asyncio.CancelledError):
                await reconciler_task
        if agent_worker_task is not None:
            agent_worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await agent_worker_task

def appended_uvicorn_run(app, *args, **kargs):
    b_lifespan = app.router.lifespan_context

    @assynccontextmanager
    async def appended_lifespan(app):
        async with b_lifespan(app) as state:
            async with custom_lifespan(app):
                yield state

    app.router.lifespan_context = appended_lifespan
    return b_uvicorn_run(app, *args, **kargs)

uvicorn.run = appended_uvicorn_run

if __name__=="__main__":
    gaia_service = GaiaService()
    gaia_service.main()
