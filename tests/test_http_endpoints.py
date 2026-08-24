import httpx

from executor_service.config import Settings
from executor_service.container import ApplicationContainer
from executor_service.interfaces.http.app import create_app


async def test_health_endpoint() -> None:
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        redis_url="redis://localhost:6399/15",
    )
    container = ApplicationContainer(settings)
    app = create_app(container)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        health = await client.get("/healthz")
        worker = await client.get("/workerz")
        removed_metrics = await client.get("/metrics")

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert worker.status_code == 200
    assert worker.json() == {
        "state": "STOPPED",
        "accepting_new_executions": False,
        "active_execution_count": 0,
    }
    assert removed_metrics.status_code == 404
    await container.redis.aclose()
    await container.engine.dispose()
