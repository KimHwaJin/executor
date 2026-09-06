"""Deletion safety, historical references and registration reuse."""

from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.runtime_targets import (
    DisableRuntimeTargetCommand,
    PurgeRuntimeTargetCommand,
)
from executor_service.application.services import ExecutionService
from executor_service.domain.enums import (
    ActorType,
    ArtifactStatus,
    ArtifactStorageType,
    ArtifactType,
    AttemptStatus,
    ExecutionStatus,
    RetryStrategy,
    RuntimePool,
    RuntimeSessionCleanupStatus,
)
from executor_service.domain.errors import (
    IdempotencyConflictError,
    RuntimeTargetNotFoundError,
    RuntimeTargetPurgeConflictError,
)
from executor_service.domain.models import utc_now
from executor_service.infrastructure.db.models import (
    ExecutionArtifactORM,
    ExecutionAttemptORM,
    ExecutionORM,
    RuntimeTargetORM,
    RuntimeTargetPurgeORM,
)
from executor_service.infrastructure.db.session import create_session_factory
from tests.test_runtime_management_safety import _registry
from tests.test_runtime_storage_access import DriverFactory, _access, _target
from tests.test_work_admission import _command


async def _history(factory, execution_id, target_id, *, session_id="kernel"):
    async with factory() as session, session.begin():
        execution = await session.get(ExecutionORM, execution_id)
        assert execution is not None
        execution.status = ExecutionStatus.SUCCEEDED
        execution.runtime_target_id = target_id
        execution.runtime_session_cleanup_status = (
            RuntimeSessionCleanupStatus.SUCCEEDED
        )
        attempt = ExecutionAttemptORM(
            heartbeat_at=utc_now(),
            started_at=utc_now(),
            execution_id=execution_id,
            attempt_number=1,
            runtime_target_id=target_id,
            runtime_session_id=session_id,
            status=AttemptStatus.SUCCEEDED,
            runtime_session_cleanup_status=RuntimeSessionCleanupStatus.SUCCEEDED,
        )
        session.add(attempt)
        await session.flush()
        artifact = ExecutionArtifactORM(
            execution_id=execution_id,
            execution_attempt_id=attempt.id,
            artifact_type=ArtifactType.REPORT,
            storage_type=ArtifactStorageType.PV,
            status=ArtifactStatus.AVAILABLE,
            name="report.md",
            uri="pv://notebooks/report.md",
            relative_path="notebooks/report.md",
            identity_hash=uuid4().hex,
        )
        session.add(artifact)
        await session.flush()
        return attempt.id, artifact.id


async def test_purge_preserves_history_and_new_registration_identity(
    engine: AsyncEngine,
    execution_service: ExecutionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, driver, registration = _registry(engine, monkeypatch)
    target = await registry.upsert(registration)
    execution = await execution_service.submit(_command("purge-history"))
    factory = create_session_factory(engine)
    attempt_id, artifact_id = await _history(factory, execution.id, target.id)
    await registry.disable(DisableRuntimeTargetCommand("disable", target.id))
    driver.reset_mock()
    command = PurgeRuntimeTargetCommand(target.id, ActorType.USER, "first")
    removed = await registry.purge(command)
    repeated = await registry.purge(replace(command, actor_id="second"))
    assert removed == repeated
    assert removed.created_by == removed.updated_by == "first"
    driver.status.assert_not_awaited()
    driver.delete_session.assert_not_awaited()
    with pytest.raises(RuntimeTargetNotFoundError):
        await registry.get(target.id)
    assert not (await registry.list()).items
    with pytest.raises(IdempotencyConflictError, match="new idempotency_key"):
        await registry.upsert(registration)
    replacement = await registry.upsert(
        replace(registration, idempotency_key="fresh-registration")
    )
    assert replacement.id != target.id
    assert replacement.name == target.name
    assert await registry.purge(command) == removed
    assert (await registry.get(replacement.id)).enabled
    assert (await execution_service.get(execution.id)).runtime_target_id == (
        target.id
    )
    async with factory() as session:
        attempt = await session.get(ExecutionAttemptORM, attempt_id)
        artifact = await session.get(ExecutionArtifactORM, artifact_id)
        tombstone = await session.scalar(select(RuntimeTargetPurgeORM))
        assert attempt is not None and attempt.runtime_target_id == target.id
        assert artifact is not None and artifact.execution_id == execution.id
        assert tombstone is not None
        assert tombstone.connection_config == registration.connection_config
        assert not hasattr(tombstone, "credential_ciphertext")
        assert await session.get(RuntimeTargetORM, target.id) is None


@pytest.mark.parametrize(
    "case",
    [
        "running",
        "waiting",
        "finalizing",
        "cancel_requested",
        "retained",
        "retained_expired",
        "cleanup_pending",
        "cleanup_failed",
        "old_attempt_cleanup",
        "different_kernel_cleaned",
    ],
)
async def test_purge_rejects_work_and_unconfirmed_cleanup(
    engine: AsyncEngine,
    execution_service: ExecutionService,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    registry, _, registration = _registry(engine, monkeypatch)
    target = await registry.upsert(registration)
    execution = await execution_service.submit(_command("purge-blocked"))
    factory = create_session_factory(engine)
    attempt_id, artifact_id = await _history(factory, execution.id, target.id)
    async with factory() as session, session.begin():
        row = await session.get(ExecutionORM, execution.id)
        attempt = await session.get(ExecutionAttemptORM, attempt_id)
        assert row is not None and attempt is not None
        row.status = ExecutionStatus.FAILED
        if case in {"running", "waiting", "finalizing", "cancel_requested"}:
            row.status = {
                "running": ExecutionStatus.RUNNING,
                "waiting": ExecutionStatus.WAITING_FOR_OPERATION,
                "finalizing": ExecutionStatus.FINALIZING,
                "cancel_requested": ExecutionStatus.CANCEL_REQUESTED,
            }[case]
            attempt.status = (
                AttemptStatus.WAITING
                if case == "waiting"
                else (AttemptStatus.RUNNING)
            )
        elif case in {"retained", "retained_expired"}:
            row.runtime_session_id = "kernel"
            row.retry_strategy = RetryStrategy.FROM_FAILED_STEP
            row.runtime_session_cleanup_status = (
                RuntimeSessionCleanupStatus.NOT_REQUIRED
            )
            row.retained_runtime_session_until = utc_now() + timedelta(
                hours=1 if case == "retained" else -1
            )
        elif case in {"cleanup_pending", "cleanup_failed"}:
            row.runtime_session_id = "kernel"
            row.runtime_session_cleanup_status = (
                RuntimeSessionCleanupStatus.PENDING
                if case == "cleanup_pending"
                else RuntimeSessionCleanupStatus.FAILED
            )
        else:
            row.runtime_target_id = None
            attempt.runtime_session_cleanup_status = (
                RuntimeSessionCleanupStatus.FAILED
            )
            if case == "different_kernel_cleaned":
                session.add(
                    ExecutionAttemptORM(
                        heartbeat_at=utc_now(),
                        started_at=utc_now(),
                        execution_id=execution.id,
                        attempt_number=2,
                        runtime_target_id=target.id,
                        runtime_session_id="different-kernel",
                        status=AttemptStatus.SUCCEEDED,
                        runtime_session_cleanup_status=RuntimeSessionCleanupStatus.SUCCEEDED,
                    )
                )
    await registry.disable(DisableRuntimeTargetCommand("disable", target.id))
    with pytest.raises(RuntimeTargetPurgeConflictError):
        await registry.purge(PurgeRuntimeTargetCommand(target.id))
    async with factory() as session:
        assert await session.get(RuntimeTargetORM, target.id) is not None
        assert await session.scalar(select(RuntimeTargetPurgeORM)) is None
        assert await session.get(ExecutionArtifactORM, artifact_id) is not None


@pytest.mark.parametrize("proof", ["later_same_kernel", "latest_execution"])
async def test_purge_allows_confirmed_cleanup_after_retained_retry(
    engine: AsyncEngine,
    execution_service: ExecutionService,
    monkeypatch: pytest.MonkeyPatch,
    proof: str,
) -> None:
    registry, _, registration = _registry(engine, monkeypatch)
    target = await registry.upsert(registration)
    execution = await execution_service.submit(_command("purge-cleaned"))
    factory = create_session_factory(engine)
    attempt_id, _ = await _history(factory, execution.id, target.id)
    async with factory() as session, session.begin():
        old = await session.get(ExecutionAttemptORM, attempt_id)
        assert old is not None
        old.status = AttemptStatus.FAILED
        old.runtime_session_cleanup_status = (
            RuntimeSessionCleanupStatus.NOT_REQUIRED
        )
        if proof == "later_same_kernel":
            session.add(
                ExecutionAttemptORM(
                    heartbeat_at=utc_now(),
                    started_at=utc_now(),
                    execution_id=execution.id,
                    attempt_number=2,
                    runtime_target_id=target.id,
                    runtime_session_id="kernel",
                    status=AttemptStatus.SUCCEEDED,
                    runtime_session_cleanup_status=RuntimeSessionCleanupStatus.SUCCEEDED,
                )
            )
    await registry.disable(DisableRuntimeTargetCommand("disable", target.id))
    await registry.purge(PurgeRuntimeTargetCommand(target.id))


@pytest.mark.parametrize("pool", list(RuntimePool))
async def test_purged_target_storage_uses_only_same_pool(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, pool: RuntimePool
) -> None:
    registry, _, command = _registry(engine, monkeypatch)
    target = await registry.upsert(replace(command, pool=pool))
    await registry.disable(DisableRuntimeTargetCommand("disable", target.id))
    await registry.purge(PurgeRuntimeTargetCommand(target.id))
    other_pool = next(p for p in RuntimePool if p != pool)
    await _target(
        engine, name="a-other", endpoint="http://other", pool=other_pool
    )
    replacement = await _target(
        engine, name="b-replacement", endpoint="http://same", pool=pool
    )
    drivers = DriverFactory({"http://same": {"cells": []}})
    access = _access(engine, drivers)
    assert (
        await access.read_notebook(
            target.runtime_type,
            target.id,
            "notebooks/execution.ipynb",
            runtime_pool=pool,
        )
    )["cells"] == []
    async with access.open_file(
        target.runtime_type, target.id, "report.md", None, runtime_pool=pool
    ) as content:
        assert b"".join([chunk async for chunk in content.body]) == b"ok"
    assert drivers.created == ["http://same", "http://same"]
    await registry.disable(
        DisableRuntimeTargetCommand("disable-new", replacement)
    )
    from executor_service.domain.runtime import RuntimeDriverError

    with pytest.raises(RuntimeDriverError, match="No healthy"):
        await access.read_notebook(
            target.runtime_type,
            target.id,
            "notebooks/execution.ipynb",
            runtime_pool=pool,
        )
