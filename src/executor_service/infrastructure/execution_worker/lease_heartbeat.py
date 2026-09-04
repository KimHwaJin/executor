"""Fenced Execution lease validation and heartbeat renewal."""

import asyncio
from datetime import timedelta

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.domain.enums import AttemptStatus, ExecutionStatus
from executor_service.domain.models import utc_now
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionORM,
)
from executor_service.infrastructure.execution_leases import (
    CancellationLease,
    ExecutionLease,
    ExecutionLeaseLostError,
    require_active_cancellation_lease,
    require_active_lease,
)
from executor_service.settings import Settings


class LeaseHeartbeatManager:
    """Validates and renews fenced execution and cancellation leases."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._heartbeat_seconds = settings.execution_heartbeat_seconds
        self._lease_seconds = settings.execution_lease_seconds

    async def assert_execution(
        self,
        lease: ExecutionLease,
        *,
        allowed_statuses: tuple[ExecutionStatus, ...] = (
            ExecutionStatus.RUNNING,
        ),
    ) -> None:
        async with self._session_factory() as session, session.begin():
            await require_active_lease(
                session,
                lease,
                allowed_statuses=allowed_statuses,
            )

    async def assert_cancellation(self, lease: CancellationLease) -> None:
        async with self._session_factory() as session, session.begin():
            await require_active_cancellation_lease(session, lease)

    async def run_execution(self, lease: ExecutionLease) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            await self.renew_execution(lease)

    async def run_cancellation(self, lease: CancellationLease) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            await self.renew_cancellation(lease)

    async def renew_cancellation(self, lease: CancellationLease) -> None:
        now = utc_now()
        lease_expires = now + timedelta(seconds=self._lease_seconds)
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                update(ExecutionORM)
                .where(
                    ExecutionORM.id == lease.execution_id,
                    ExecutionORM.status == ExecutionStatus.CANCEL_REQUESTED,
                    ExecutionORM.cancellation_lease_owner == lease.owner,
                    ExecutionORM.fencing_token == lease.fencing_token,
                    ExecutionORM.cancellation_lease_expires_at.is_not(None),
                    ExecutionORM.cancellation_lease_expires_at > now,
                )
                .values(
                    cancellation_heartbeat_at=now,
                    cancellation_lease_expires_at=lease_expires,
                    updated_at=now,
                )
            )
            if getattr(result, "rowcount", None) != 1:
                raise ExecutionLeaseLostError(
                    f"Execution {lease.execution_id} cancellation heartbeat "
                    f"lost fence {lease.fencing_token}."
                )

    async def renew_execution(self, lease: ExecutionLease) -> None:
        now = utc_now()
        lease_expires = now + timedelta(seconds=self._lease_seconds)
        async with self._session_factory() as session, session.begin():
            execution_update = await session.execute(
                update(ExecutionORM)
                .where(
                    ExecutionORM.id == lease.execution_id,
                    ExecutionORM.status == ExecutionStatus.RUNNING,
                    ExecutionORM.lease_owner == lease.owner,
                    ExecutionORM.fencing_token == lease.fencing_token,
                    ExecutionORM.lease_expires_at.is_not(None),
                    ExecutionORM.lease_expires_at > now,
                )
                .values(
                    heartbeat_at=now,
                    lease_expires_at=lease_expires,
                    updated_at=now,
                )
            )
            if getattr(execution_update, "rowcount", None) != 1:
                raise ExecutionLeaseLostError(
                    f"Execution {lease.execution_id} heartbeat lost fence "
                    f"{lease.fencing_token}."
                )
            attempt_update = await session.execute(
                update(ExecutionAttemptORM)
                .where(
                    ExecutionAttemptORM.id == lease.attempt_id,
                    ExecutionAttemptORM.status == AttemptStatus.RUNNING,
                    ExecutionAttemptORM.lease_owner == lease.owner,
                    ExecutionAttemptORM.fencing_token == lease.fencing_token,
                    ExecutionAttemptORM.lease_expires_at.is_not(None),
                    ExecutionAttemptORM.lease_expires_at > now,
                )
                .values(
                    heartbeat_at=now,
                    lease_expires_at=lease_expires,
                )
            )
            if getattr(attempt_update, "rowcount", None) != 1:
                raise ExecutionLeaseLostError(
                    f"Execution Attempt {lease.attempt_id} heartbeat lost "
                    f"fence {lease.fencing_token}."
                )
