"""Heartbeat loops survive database blips and stop on a lost fence."""

from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.infrastructure.execution_leases import (
    CancellationLease,
    ExecutionLease,
    ExecutionLeaseLostError,
)
from executor_service.infrastructure.execution_worker.lease_heartbeat import (
    LeaseHeartbeatManager,
)
from executor_service.settings import Settings


def _manager() -> LeaseHeartbeatManager:
    manager = LeaseHeartbeatManager(
        cast(async_sessionmaker[AsyncSession], None),
        Settings(_env_file=None),
    )
    manager._heartbeat_seconds = 0
    return manager


def _renewals() -> AsyncMock:
    # A database blip, a recovered renewal, then another Worker's fence.
    return AsyncMock(
        side_effect=[
            OperationalError("UPDATE", {}, Exception("connection reset")),
            None,
            ExecutionLeaseLostError("fence moved"),
        ]
    )


async def test_execution_heartbeat_retries_after_a_database_error() -> None:
    manager = _manager()
    renew = _renewals()
    manager.renew_execution = renew
    lease = ExecutionLease(uuid4(), uuid4(), "worker", 1)

    with pytest.raises(ExecutionLeaseLostError):
        await manager.run_execution(lease)

    assert renew.await_count == 3


async def test_cancellation_heartbeat_retries_after_a_database_error() -> None:
    manager = _manager()
    renew = _renewals()
    manager.renew_cancellation = renew
    lease = CancellationLease(uuid4(), "worker", 1)

    with pytest.raises(ExecutionLeaseLostError):
        await manager.run_cancellation(lease)

    assert renew.await_count == 3
