"""Redis transport deadlines shared by startup, workers and publication."""

from typing import Any

from redis.asyncio import ConnectionPool, Redis
from redis.asyncio.connection import parse_url
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from executor_service.settings import Settings


def create_redis_client(settings: Settings) -> Redis:
    options: dict[str, Any] = dict(parse_url(settings.redis_dsn))
    # Explicit service settings win over URL query overrides (including zero
    # timeouts). Outbox/consumer recovery owns retries, not the socket driver.
    options.update(
        decode_responses=True,
        socket_connect_timeout=settings.redis_connect_timeout_seconds,
        socket_timeout=settings.redis_socket_timeout_seconds,
        retry=Retry(NoBackoff(), 0),
    )
    return Redis.from_pool(ConnectionPool(**options))
