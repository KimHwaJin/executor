from pydantic import BaseModel, Field


class RedisPingResource(BaseModel):
    """Workflow Redis broker 연결 확인 결과."""

    ok: bool
    response: str = Field(description="Redis PING 응답 (보통 PONG)")
    latency_ms: int
    host: str
    port: int
    db: int
    redis_version: str | None = None

