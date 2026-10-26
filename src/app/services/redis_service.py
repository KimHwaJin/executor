from __future__ import annotations

import time
from urllib.parse import urlparse

from fastapi import HTTPException
from redis.asyncio import Redis
from redis.exceptions import RedisError

from config import settings
from app.schemas.common.redis_schema import RedisPingResource


class RedisService:
    """Workflow 송수신용 Redis broker 연결을 검증합니다."""

    @staticmethod
    def _endpoint() -> tuple[str, int, int]:
        parsed = urlparse(settings.redis_url)
        if parsed.scheme not in {"redis", "rediss"}:
            raise HTTPException(status_code=503, detail="REDIS_URL must use redis:// or rediss://.")
        if not parsed.hostname:
            raise HTTPException(status_code=503, detail="REDIS_URL host is missing.")
        host = parsed.hostname
        port = parsed.port or 6379
        db = 0
        if parsed.path and parsed.path != "/":
            try:
                db = int(parsed.path.lstrip("/"))
            except ValueError as exc:
                raise HTTPException(status_code=503, detail="REDIS_URL db index is invalid.") from exc
        return host, port, db

    @staticmethod
    async def ping() -> RedisPingResource:
        host, port, db = RedisService._endpoint()
        client = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=settings.redis_ping_timeout_seconds,
            socket_timeout=settings.redis_ping_timeout_seconds,
        )
        started = time.monotonic()
        try:
            response = await client.ping()
            if response is not True:
                raise HTTPException(status_code=503, detail=f"Unexpected Redis PING response: {response!r}")
            info = await client.info("server")
            latency_ms = int((time.monotonic() - started) * 1000)
            return RedisPingResource(
                ok=True,
                response="PONG",
                latency_ms=latency_ms,
                host=host,
                port=port,
                db=db,
                redis_version=str(info.get("redis_version")) if info.get("redis_version") else None,
            )
        except HTTPException:
            raise
        except RedisError as exc:
            raise HTTPException(status_code=503, detail=f"Redis ping failed: {exc}") from exc
        finally:
            await client.aclose()
