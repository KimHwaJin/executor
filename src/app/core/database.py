from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from config import settings


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """
    Engine을 최초 DB 사용 시점에 생성합니다.

    이렇게 하면 OpenAPI 문서 생성이나 정적 import 단계에서는 asyncpg 연결을
    요구하지 않고, 실제 요청 처리 시에만 DB 드라이버가 필요합니다.
    """

    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings.database_url,
            echo=settings.sql_echo,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            pool_timeout=settings.database_pool_timeout_seconds,
            pool_recycle=settings.database_pool_recycle_seconds,
            pool_pre_ping=True,
            # Local PostgreSQL is configured without TLS.  asyncpg otherwise
            # attempts TLS negotiation before the first repository query,
            # which causes the server-side connection reset observed on Windows.
            connect_args={
                "ssl": False,
                # SQLAlchemy's asyncpg dialect otherwise caches prepared
                # statements per physical connection.  Disable that cache for
                # this local PostgreSQL instance while diagnosing resets.
                "prepared_statement_cache_size": 0,
            },
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI Depends에서 사용할 비동기 DB Session."""

    async with get_session_factory()() as db:
        try:
            yield db
        except Exception:
            await db.rollback()
            raise
