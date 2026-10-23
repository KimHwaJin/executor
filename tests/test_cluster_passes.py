"""Cluster-wide throttling of background passes."""

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.domain.models import utc_now
from executor_service.infrastructure.cluster_passes import claim_pass
from executor_service.infrastructure.db.models import BackgroundPassLeaseORM
from executor_service.infrastructure.db.session import create_session_factory


async def test_one_pod_claims_each_interval(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)

    assert await claim_pass(factory, "audit", "pod-a", 60)
    assert not await claim_pass(factory, "audit", "pod-b", 60)
    assert not await claim_pass(factory, "audit", "pod-a", 60)
    # Keys are independent.
    assert await claim_pass(factory, "health", "pod-b", 60)

    async with factory() as session, session.begin():
        await session.execute(
            update(BackgroundPassLeaseORM)
            .where(BackgroundPassLeaseORM.singleton_key == "audit")
            .values(lease_expires_at=utc_now())
        )
    assert await claim_pass(factory, "audit", "pod-b", 60)
