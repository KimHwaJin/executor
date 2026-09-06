"""Operator intent and in-use Runtime identity must survive probes/updates."""

from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.runtime_targets import (
    SetRuntimeTargetStateCommand,
    UpsertRuntimeTargetCommand,
)
from executor_service.application.services import ExecutionService
from executor_service.domain.enums import (
    AttemptStatus,
    ExecutionStatus,
    RetryStrategy,
    RuntimePool,
    RuntimeSessionCleanupStatus,
    RuntimeTargetStatus,
)
from executor_service.domain.errors import RuntimeTargetConfigurationError
from executor_service.domain.models import utc_now
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.runtime_registry import (
    RuntimeTargetRegistry,
)
from executor_service.settings import Settings
from tests.test_work_admission import _command


def _registry(engine, monkeypatch):
    registry = RuntimeTargetRegistry(
        create_session_factory(engine),
        Settings(_env_file=None, runtime_allowed_profiles=("basic",)),
    )
    driver = AsyncMock()
    driver.status.return_value = {"active_session_count": 0}
    driver.supported_profiles.return_value = ["basic"]
    driver.resource_status.return_value = None
    monkeypatch.setattr(
        registry._prober._driver_factory, "create", lambda *args: driver
    )
    command = UpsertRuntimeTargetCommand(
        idempotency_key=str(uuid4()),
        name="safe-target",
        connection_config={"endpoint": "http://old.invalid:8888"},
        credential="test-credential",
        pool=RuntimePool.INTERACTIVE,
    )
    return registry, driver, command


async def test_drain_survives_outage_and_recovery(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, driver, command = _registry(engine, monkeypatch)
    target = await registry.upsert(command)
    await registry.set_state(
        SetRuntimeTargetStateCommand(
            idempotency_key="drain",
            target_id=target.id,
            desired_state=RuntimeTargetStatus.DRAINING,
        )
    )
    driver.status.side_effect = ConnectionError("unavailable")
    failed = await registry.probe(target.id)
    assert failed.status == RuntimeTargetStatus.DRAINING
    assert failed.last_health_error is not None
    driver.status.side_effect = None
    healthy = await registry.probe(target.id)
    assert healthy.status == RuntimeTargetStatus.DRAINING
    assert healthy.last_health_error is None
    assert not healthy.accepting_new_executions
    active = await registry.set_state(
        SetRuntimeTargetStateCommand(
            idempotency_key="activate",
            target_id=target.id,
            desired_state=RuntimeTargetStatus.ACTIVE,
        )
    )
    assert active.accepting_new_executions


@pytest.mark.parametrize(
    "reservation", ["running", "waiting", "retained", "cleanup"]
)
async def test_in_use_target_identity_is_immutable(
    engine: AsyncEngine,
    execution_service: ExecutionService,
    monkeypatch: pytest.MonkeyPatch,
    reservation: str,
) -> None:
    registry, _driver, command = _registry(engine, monkeypatch)
    target = await registry.upsert(command)
    execution = await execution_service.submit(_command("reserved"))
    factory = create_session_factory(engine)
    async with factory() as session, session.begin():
        if reservation in {"running", "waiting"}:
            session.add(
                ExecutionAttemptORM(
                    execution_id=execution.id,
                    attempt_number=1,
                    runtime_target_id=target.id,
                    status=AttemptStatus.RUNNING
                    if reservation == "running"
                    else AttemptStatus.WAITING,
                    lease_owner="worker",
                    fencing_token=1,
                    started_at=utc_now(),
                    heartbeat_at=utc_now(),
                    lease_expires_at=utc_now() + timedelta(minutes=1),
                )
            )
        else:
            await session.execute(
                update(ExecutionORM)
                .where(ExecutionORM.id == execution.id)
                .values(
                    runtime_target_id=target.id,
                    runtime_session_id="kernel",
                    status=ExecutionStatus.FAILED,
                    retry_strategy=RetryStrategy.FROM_FAILED_STEP,
                    retained_runtime_session_until=(
                        utc_now() + timedelta(hours=1)
                        if reservation == "retained"
                        else None
                    ),
                    runtime_session_cleanup_status=(
                        RuntimeSessionCleanupStatus.PENDING
                        if reservation == "cleanup"
                        else RuntimeSessionCleanupStatus.NOT_REQUIRED
                    ),
                )
            )
    for changes in (
        {"pool": RuntimePool.BATCH},
        {"connection_config": {"endpoint": "http://new.invalid:8888"}},
    ):
        with pytest.raises(
            RuntimeTargetConfigurationError, match="reservations"
        ):
            await registry.upsert(
                replace(command, idempotency_key=str(uuid4()), **changes)
            )
    # Capacity and token rotation do not change which server owns a kernel.
    refreshed = await registry.upsert(
        replace(
            command,
            idempotency_key=str(uuid4()),
            credential="rotated",
            max_concurrent_executions=4,
        )
    )
    assert refreshed.max_concurrent_executions == 4


async def test_stale_probe_cannot_validate_reconfigured_endpoint(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, driver, command = _registry(engine, monkeypatch)
    target = await registry.upsert(command)
    factory = create_session_factory(engine)

    async def reconfigure_during_probe():
        async with factory() as session, session.begin():
            await session.execute(
                update(RuntimeTargetORM)
                .where(RuntimeTargetORM.id == target.id)
                .values(
                    connection_config={"endpoint": "http://new.invalid:8888"},
                    status=RuntimeTargetStatus.OFFLINE,
                    supported_profiles=[],
                    last_health_error="new endpoint not probed",
                )
            )
        return {"active_session_count": 0}

    driver.status.side_effect = reconfigure_during_probe
    result = await registry.probe(target.id)
    assert result.status == RuntimeTargetStatus.OFFLINE
    assert result.last_health_error == "new endpoint not probed"
    assert result.supported_profiles == ()
