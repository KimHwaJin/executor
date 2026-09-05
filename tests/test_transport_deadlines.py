"""Bound network stalls without a running Redis or production services."""

import asyncio
from typing import cast

import pytest
from redis.asyncio import Redis
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.services import ExecutionService
from executor_service.domain.enums import OutboxStatus
from executor_service.infrastructure.db.models import OutboxEventORM
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.outbox import OutboxPublisher
from executor_service.infrastructure.redis_client import create_redis_client
from executor_service.settings import Settings
from tests.test_events import RecordingRedis, _submit_command


async def test_redis_blackhole_has_a_socket_deadline() -> None:
    writers = []

    async def blackhole(reader, writer):
        writers.append(writer)
        await reader.read()

    server = await asyncio.start_server(blackhole, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = create_redis_client(
        Settings(
            _env_file=None,
            redis_url=f"redis://127.0.0.1:{port}/0?socket_timeout=0",
            redis_socket_timeout_seconds=1.05,
            redis_connect_timeout_seconds=0.2,
        )
    )
    try:
        assert (
            client.connection_pool.connection_kwargs["socket_timeout"] == 1.05
        )
        async with asyncio.timeout(3):
            with pytest.raises(RedisTimeoutError):
                await client.ping()
    finally:
        await client.aclose()
        for writer in writers:
            writer.close()
            await writer.wait_closed()
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("shutdown", [False, True])
async def test_outbox_stall_leaves_retryable_db_state(
    execution_service: ExecutionService, engine: AsyncEngine, shutdown: bool
) -> None:
    await execution_service.submit(_submit_command())
    entered = asyncio.Event()

    class StalledRedis:
        async def xadd(self, *args):
            entered.set()
            await asyncio.Event().wait()

    factory = create_session_factory(engine)
    publisher = OutboxPublisher(
        factory,
        cast(Redis, StalledRedis()),
        "work",
        "events",
        0.01,
        100,
        publish_timeout_seconds=60 if shutdown else 0.02,
        shutdown_timeout_seconds=0.02,
    )
    async with asyncio.timeout(2):
        if shutdown:
            publisher.start()
            await entered.wait()
            await publisher.stop()
            assert publisher._task is None
        else:
            assert await publisher.publish_batch() == 0
    async with factory() as session, session.begin():
        row = await session.scalar(select(OutboxEventORM))
        assert row is not None
        assert row.status == OutboxStatus.PENDING
        assert row.attempt_count == (0 if shutdown else 1)
        row.available_at = row.created_at
    # Both deadline and shutdown paths release DB transaction resources.
    recovered = OutboxPublisher(
        factory, cast(Redis, RecordingRedis()), "work", "events", 0.01, 100
    )
    assert await recovered.publish_batch() == 1


@pytest.mark.parametrize("heartbeat", [30, 60])
def test_invalid_lease_heartbeat_configuration_is_rejected(heartbeat: int):
    with pytest.raises(ValueError, match="HEARTBEAT"):
        Settings(
            _env_file=None,
            execution_lease_seconds=30,
            execution_heartbeat_seconds=heartbeat,
        )
