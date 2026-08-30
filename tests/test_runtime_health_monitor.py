"""Runtime health monitor lifecycle and target-isolation tests."""

import asyncio
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.domain.enums import (
    RuntimePool,
    RuntimeTargetStatus,
)
from executor_service.infrastructure._runtime_registry.monitor import (
    RuntimeHealthMonitor,
)
from executor_service.infrastructure.db.models import RuntimeTargetORM
from executor_service.infrastructure.db.session import create_session_factory
from tests.runtime_credentials import runtime_credential_fields


class RecordingProber:
    def __init__(self, expected_calls: int) -> None:
        self.calls: list[UUID] = []
        self.fail_target_id: UUID | None = None
        self.completed = asyncio.Event()
        self._expected_calls = expected_calls

    async def probe(self, target_id: UUID) -> object:
        self.calls.append(target_id)
        if len(self.calls) >= self._expected_calls:
            self.completed.set()
        if target_id == self.fail_target_id:
            raise RuntimeError("expected probe failure")
        return object()


def _target(name: str, *, enabled: bool) -> RuntimeTargetORM:
    return RuntimeTargetORM(
        name=name,
        connection_config={"endpoint": f"http://{name}.invalid:8888"},
        **runtime_credential_fields(),
        pool=RuntimePool.INTERACTIVE,
        status=RuntimeTargetStatus.ACTIVE,
        max_concurrent_executions=1,
        supported_profiles=["basic"],
        enabled=enabled,
    )


async def test_monitor_probes_enabled_targets_and_isolates_failures(
    engine: AsyncEngine,
) -> None:
    session_factory = create_session_factory(engine)
    first = _target("monitor-first", enabled=True)
    second = _target("monitor-second", enabled=True)
    disabled = _target("monitor-disabled", enabled=False)
    async with session_factory() as session, session.begin():
        session.add_all([first, second, disabled])
        await session.flush()
        expected_ids = {first.id, second.id}
        disabled_id = disabled.id

    prober = RecordingProber(expected_calls=2)
    prober.fail_target_id = first.id
    monitor = RuntimeHealthMonitor(session_factory, 60, prober)

    await monitor.start()
    try:
        await asyncio.wait_for(prober.completed.wait(), timeout=1)
    finally:
        await monitor.stop()

    assert set(prober.calls) == expected_ids
    assert disabled_id not in prober.calls


async def test_monitor_start_and_stop_are_idempotent(
    engine: AsyncEngine,
) -> None:
    monitor = RuntimeHealthMonitor(
        create_session_factory(engine),
        60,
        RecordingProber(expected_calls=1),
    )

    await monitor.stop()
    await monitor.start()
    task = monitor._task
    await monitor.start()

    assert task is not None
    assert monitor._task is task

    await monitor.stop()
    await monitor.stop()

    assert monitor._task is None
