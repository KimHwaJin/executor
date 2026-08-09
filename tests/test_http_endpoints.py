import httpx

from executor_service.config import Settings
from executor_service.container import ApplicationContainer
from executor_service.interfaces.http.app import create_app


async def test_health_and_metrics_endpoints() -> None:
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        redis_url="redis://localhost:6399/15",
    )
    container = ApplicationContainer(settings)
    app = create_app(container)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        health = await client.get("/healthz")
        metrics = await client.get("/metrics")

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert metrics.status_code == 200
    assert "executor_mcp_tool_calls_total" in metrics.text
    assert "executor_outbox_pending_events" in metrics.text
    assert "executor_stream_pending_messages" in metrics.text
    assert "executor_worker_active_jobs" in metrics.text
    assert "executor_jupyter_pool_capacity" in metrics.text
    assert "executor_jupyter_pool_queued_executions" in metrics.text
    await container.redis.aclose()
    await container.engine.dispose()
