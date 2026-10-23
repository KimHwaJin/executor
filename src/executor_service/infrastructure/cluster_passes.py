"""Run each background pass on one Pod per interval, cluster-wide."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta

from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.domain.models import utc_now
from executor_service.infrastructure.db.models import BackgroundPassLeaseORM


async def claim_pass(
    session_factory: async_sessionmaker[AsyncSession],
    key: str,
    owner: str,
    interval_seconds: float,
) -> bool:
    """Claim the next pass of ``key`` unless another Pod ran it recently.

    Every pass is safe to overlap, so the claim only throttles: a Pod that
    dies mid-pass delays the next one by at most one interval.
    """
    now = utc_now()
    async with session_factory() as session, session.begin():
        if await session.get(BackgroundPassLeaseORM, key) is None:
            try:
                async with session.begin_nested():
                    session.add(
                        BackgroundPassLeaseORM(
                            singleton_key=key, created_at=now, updated_at=now
                        )
                    )
            except IntegrityError:
                pass
        result = await session.execute(
            update(BackgroundPassLeaseORM)
            .where(
                BackgroundPassLeaseORM.singleton_key == key,
                or_(
                    BackgroundPassLeaseORM.lease_expires_at.is_(None),
                    BackgroundPassLeaseORM.lease_expires_at <= now,
                ),
            )
            .values(
                lease_owner=owner,
                lease_expires_at=now + timedelta(seconds=interval_seconds),
                last_started_at=now,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        return getattr(result, "rowcount", None) == 1


async def run_cluster_pass_loop(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    key: str,
    owner: str,
    interval_seconds: float,
    run_pass: Callable[[], Awaitable[object]],
    on_error: Callable[[Exception], None],
) -> None:
    """Poll every interval and run ``run_pass`` when this Pod claims it."""
    while True:
        try:
            if await claim_pass(session_factory, key, owner, interval_seconds):
                await run_pass()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            on_error(exc)
        await asyncio.sleep(interval_seconds)
