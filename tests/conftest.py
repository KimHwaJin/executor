"""Test fixtures using a real SQLAlchemy repository over isolated SQLite."""

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.services import ExecutionService
from executor_service.infrastructure.db.base import Base
from executor_service.infrastructure.db.repositories import SQLAlchemyUnitOfWork
from executor_service.infrastructure.db.session import create_engine, create_session_factory


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    test_engine = create_engine("sqlite+aiosqlite:///:memory:")
    async with test_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield test_engine
    await test_engine.dispose()


@pytest_asyncio.fixture
async def execution_service(engine: AsyncEngine) -> ExecutionService:
    session_factory = create_session_factory(engine)
    return ExecutionService(lambda: SQLAlchemyUnitOfWork(session_factory))
