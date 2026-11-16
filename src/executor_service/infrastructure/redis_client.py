"""Redis client shared by publication, retention and health checks."""

from typing import Any

from redis.asyncio import ConnectionPool, Redis
from redis.asyncio.connection import parse_url
from redis.asyncio.retry import Retry
from redis.asyncio.sentinel import Sentinel, SentinelConnectionPool
from redis.backoff import NoBackoff

from executor_service.settings import Settings


def create_redis_client(settings: Settings) -> Redis:
    # Explicit service settings win over URL query overrides (including zero
    # timeouts). The Outbox owns retries, not the socket driver.
    transport: dict[str, Any] = {
        "decode_responses": True,
        "socket_connect_timeout": settings.redis_connect_timeout_seconds,
        "socket_timeout": settings.redis_socket_timeout_seconds,
        "retry": Retry(NoBackoff(), 0),
    }
    if settings.redis_mode == "standalone":
        options: dict[str, Any] = dict(parse_url(settings.redis_dsn))
        return Redis.from_pool(ConnectionPool(**(options | transport)))
    # Settings validation guarantees these in sentinel mode.
    assert settings.redis_sentinel_host is not None
    assert settings.redis_sentinel_master_name is not None
    assert settings.redis_sentinel_auth is not None
    account = transport | {
        "username": settings.redis_sentinel_username,
        "password": _decrypt(settings.redis_sentinel_auth.get_secret_value()),
    }
    # Each new connection asks Sentinel for the current master, so a
    # failover needs no restart.
    sentinel = Sentinel(
        [(settings.redis_sentinel_host, settings.redis_sentinel_port)],
        sentinel_kwargs=account,
        **account,
    )
    return sentinel.master_for(
        settings.redis_sentinel_master_name,
        connection_pool_class=_SentinelPool,
    )


class _SentinelPool(SentinelConnectionPool):
    """Closing the client also closes its connections to Sentinel."""

    async def disconnect(self, inuse_connections: bool = True) -> None:
        await super().disconnect(inuse_connections)
        for sentinel in self.sentinel_manager.sentinels:
            await sentinel.aclose()


def _decrypt(value: str) -> str:
    try:
        from hcp.framework.util.crypto_cipher import (  # ty: ignore[unresolved-import]
            AESCipher,
        )
    except ImportError:
        raise RuntimeError(
            "REDIS_SENTINEL_AUTH needs the internal hcp library to decrypt."
        ) from None
    return AESCipher().decrypt_str(value)
