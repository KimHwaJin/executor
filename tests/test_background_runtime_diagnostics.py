"""Fault injection for terminal cleanup and retained MULTI observations."""

import asyncio
import errno
from datetime import timedelta
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.commands import RetryExecutionCommand
from executor_service.application.services import ExecutionService
from executor_service.domain.diagnostics import DiagnosticCategory
from executor_service.domain.enums import (
    AttemptStatus,
    ExecutionStatus,
    FailureType,
    OperationMode,
    RetryStrategy,
    RuntimeSessionCleanupStatus,
)
from executor_service.domain.errors import InvalidStateTransitionError
from executor_service.domain.models import utc_now
from executor_service.domain.runtime import RuntimeDriverError
from executor_service.infrastructure.background_diagnostics import (
    BackgroundDiagnosticRecorder,
    RuntimeObservation,
)
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionORM,
)
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.diagnostic_store import (
    SQLAlchemyDiagnosticQueryService,
)
from tests.test_multi_lifecycle import (
    _make_waiting,
    _patch_runtime_driver,
    _worker,
)


async def _observation(
    service: ExecutionService, engine: AsyncEngine, *, terminal: bool = False
) -> RuntimeObservation:
    execution, attempt = await _make_waiting(service, engine, str(uuid4()))
    factory = create_session_factory(engine)
    async with factory() as session, session.begin():
        row = await session.get(ExecutionORM, execution.id)
        history = await session.get(ExecutionAttemptORM, attempt.id)
        assert row is not None and history is not None
        row.fencing_token = history.fencing_token = 1
        if terminal:
            row.status = ExecutionStatus.FAILED
            row.failure_type = FailureType.TOOL_ERROR
            row.error_message = "primary tool error"
            row.retry_strategy = RetryStrategy.FROM_START
            row.runtime_session_cleanup_status = (
                RuntimeSessionCleanupStatus.PENDING
            )
            history.status = AttemptStatus.FAILED
            history.failure_type = FailureType.TOOL_ERROR
            history.error_message = row.error_message
        return RuntimeObservation.capture(row)


class BackgroundDriver:
    fault: ClassVar[str] = ""
    close_fails: ClassVar[bool] = False
    deleted: ClassVar[list[str]] = []
    probed: ClassVar[list[str]] = []

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        if self.fault == "create":
            raise ValueError("private password=do-not-log")

    async def delete_session(self, session_id: str) -> None:
        self.deleted.append(session_id)
        if self.fault == "delete":
            raise PermissionError(errno.EACCES, "private-secret")

    async def session_exists(self, session_id: str) -> bool:
        self.probed.append(session_id)
        if self.fault == "probe":
            raise RuntimeDriverError("Runtime status=503 password=do-not-log")
        return self.fault != "missing"

    async def close(self) -> None:
        if self.close_fails:
            raise OSError(errno.EIO, "private-secret")


def _driver(
    monkeypatch: pytest.MonkeyPatch, fault: str = "", close_fails: bool = False
) -> None:
    BackgroundDriver.fault = fault
    BackgroundDriver.close_fails = close_fails
    BackgroundDriver.deleted = []
    BackgroundDriver.probed = []
    _patch_runtime_driver(monkeypatch, BackgroundDriver)


async def test_background_dedup_preserves_state_and_records_changed_cause(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation = await _observation(execution_service, engine)
    factory = create_session_factory(engine)
    query = SQLAlchemyDiagnosticQueryService(factory)
    first, second = (
        BackgroundDiagnosticRecorder(factory),
        BackgroundDiagnosticRecorder(factory),
    )
    error = RuntimeDriverError("upstream unavailable")
    kwargs = {
        "phase": "MULTI_SESSION_PROBE",
        "category": DiagnosticCategory.EXECUTION,
    }
    assert await first.record(observation, error, **kwargs)
    assert not await first.record(observation, error, **kwargs)
    assert not await second.record(observation, error, **kwargs)
    assert await second.record(
        observation, PermissionError(errno.EACCES, "private"), **kwargs
    )
    items = (await query.list(observation.execution_id)).items
    assert len(items) == 2
    assert {item.diagnostic.code for item in items} == {
        "RUNTIME_UNAVAILABLE",
        "PERMISSION_DENIED",
    }
    async with factory() as session:
        assert await observation.current(session) is not None
    future = utc_now() + timedelta(seconds=301)
    monkeypatch.setattr(
        "executor_service.infrastructure.background_diagnostics.utc_now",
        lambda: future,
    )
    # A fresh process still consults the durable window, not just its cache.
    assert await BackgroundDiagnosticRecorder(factory).record(
        observation, error, **kwargs
    )
    assert len((await query.list(observation.execution_id)).items) == 3


@pytest.mark.parametrize(
    "change", ["version", "fence", "session", "target", "status", "operation"]
)
async def test_stale_snapshot_cannot_record_delete_or_update_new_state(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    observation = await _observation(execution_service, engine, terminal=True)
    factory = create_session_factory(engine)
    values: dict[str, Any] = {
        "version": {"version": observation.version + 1},
        "fence": {"fencing_token": observation.fencing_token + 1},
        "session": {"runtime_session_id": "replacement-kernel"},
        "target": {"runtime_target_id": None},
        "status": {"status": ExecutionStatus.RUNNING},
        "operation": {"active_operation_id": None},
    }[change]
    async with factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == observation.execution_id)
            .values(**values)
        )
    _driver(monkeypatch)
    worker, redis = _worker(engine, tmp_path)
    try:
        assert not await worker._session_recovery.diagnostics.record(
            observation,
            RuntimeDriverError("stale"),
            phase="RECOVERY_SESSION_DELETE",
            category=DiagnosticCategory.CLEANUP,
        )
        await worker._session_recovery.cleanup(observation)
        await worker._session_recovery.record_result(
            observation, RuntimeSessionCleanupStatus.SUCCEEDED
        )
    finally:
        await redis.aclose()
    assert BackgroundDriver.deleted == []
    assert not (
        await SQLAlchemyDiagnosticQueryService(factory).list(
            observation.execution_id
        )
    ).items
    async with factory() as session:
        row = await session.get(ExecutionORM, observation.execution_id)
        assert row is not None
        assert (
            row.runtime_session_cleanup_status
            == RuntimeSessionCleanupStatus.PENDING
        )


@pytest.mark.parametrize("fault", ["create", "delete", "target", "close"])
async def test_cleanup_records_specific_cause_without_replacing_primary_failure(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fault: str,
) -> None:
    observation = await _observation(execution_service, engine, terminal=True)
    factory = create_session_factory(engine)
    if fault == "target":
        async with factory() as session, session.begin():
            row = await session.get(ExecutionORM, observation.execution_id)
            assert row is not None
            row.runtime_target_id = None
            observation = RuntimeObservation.capture(row)
    _driver(monkeypatch, fault, close_fails=fault in {"close", "delete"})
    worker, redis = _worker(engine, tmp_path)
    try:
        await worker._session_recovery.cleanup(observation)
    finally:
        await redis.aclose()
    diagnostics = (
        await SQLAlchemyDiagnosticQueryService(factory).list(
            observation.execution_id
        )
    ).items
    phases = {item.diagnostic.phase for item in diagnostics}
    assert {
        "create": "RECOVERY_DRIVER_CREATE",
        "delete": "RECOVERY_SESSION_DELETE",
        "target": "RECOVERY_TARGET",
        "close": "RECOVERY_DRIVER_CLOSE",
    }[fault] in phases
    if fault == "delete":
        assert "RECOVERY_DRIVER_CLOSE" in phases
    assert (
        "private-secret" not in caplog.text and "do-not-log" not in caplog.text
    )
    async with factory() as session:
        row = await session.get(ExecutionORM, observation.execution_id)
        assert row is not None and row.failure_type == FailureType.TOOL_ERROR
        assert row.error_message == "primary tool error"
        assert row.runtime_session_cleanup_status == (
            RuntimeSessionCleanupStatus.SUCCEEDED
            if fault == "close"
            else RuntimeSessionCleanupStatus.FAILED
        )
        assert (row.runtime_session_id is None) == (fault == "close")


@pytest.mark.parametrize("fault", ["probe", "create", "missing"])
async def test_multi_observation_is_bounded_and_transport_close_does_not_mask_probe(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    observation = await _observation(execution_service, engine)
    _driver(monkeypatch, fault, close_fails=True)
    worker, redis = _worker(engine, tmp_path)
    try:
        await worker._multi_lifecycle.audit()
        await worker._multi_lifecycle.audit()
    finally:
        await redis.aclose()
    factory = create_session_factory(engine)
    diagnostics = (
        await SQLAlchemyDiagnosticQueryService(factory).list(
            observation.execution_id
        )
    ).items
    phases = [item.diagnostic.phase for item in diagnostics]
    assert len(phases) == len(set(phases))
    async with factory() as session:
        row = await session.get(ExecutionORM, observation.execution_id)
        assert row is not None
        if fault == "missing":
            assert row.status == ExecutionStatus.FAILED
            assert row.failure_type == FailureType.RUNTIME_SESSION_LOST
        else:
            assert row.status == ExecutionStatus.WAITING_FOR_OPERATION
            assert row.version == observation.version
            assert row.failure_type is None
            assert (
                "MULTI_SESSION_PROBE"
                if fault == "probe"
                else "MULTI_DRIVER_CREATE"
            ) in phases


async def test_expired_cleanup_is_reserved_before_delete_and_batch_survives_one_fault(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations = [
        await _observation(execution_service, engine, terminal=True)
        for _ in range(2)
    ]
    factory = create_session_factory(engine)
    for observation in observations:
        async with factory() as session, session.begin():
            row = await session.get(ExecutionORM, observation.execution_id)
            assert row is not None
            row.retry_strategy = RetryStrategy.FROM_FAILED_STEP
            row.operation_mode = OperationMode.SINGLE
            row.operation_wait_timeout_seconds = None
            row.operation_wait_expires_at = None
            row.retry_from_sequence = 0
            row.retained_runtime_session_until = utc_now() - timedelta(
                seconds=1
            )
            row.runtime_session_cleanup_status = (
                RuntimeSessionCleanupStatus.NOT_REQUIRED
            )
    _driver(monkeypatch)
    worker, redis = _worker(engine, tmp_path)
    seen = []

    async def delete(_self: Any, session_id: str) -> None:
        seen.append(session_id)
        observation = next(
            value for value in observations if value.session_id == session_id
        )
        async with factory() as session:
            row = await session.get(ExecutionORM, observation.execution_id)
            assert row is not None
            assert row.retry_strategy == RetryStrategy.NOT_RETRYABLE
            assert (
                row.runtime_session_cleanup_status
                == RuntimeSessionCleanupStatus.PENDING
            )
        with pytest.raises(
            InvalidStateTransitionError, match="no supported retry strategy"
        ):
            await execution_service.retry(
                RetryExecutionCommand(observation.execution_id, str(uuid4()))
            )
        if session_id == observations[0].session_id:
            raise RuntimeDriverError("delete unavailable")

    monkeypatch.setattr(BackgroundDriver, "delete_session", delete)
    try:
        await worker._retained_session_cleaner.cleanup_expired()
        await worker._retained_session_cleaner.cleanup_expired()
    finally:
        await redis.aclose()
    assert len(seen) == 2
    async with factory() as session:
        first = await session.get(ExecutionORM, observations[0].execution_id)
        second = await session.get(ExecutionORM, observations[1].execution_id)
        assert first is not None and second is not None
        assert (
            first.runtime_session_cleanup_status
            == RuntimeSessionCleanupStatus.FAILED
        )
        assert (
            second.runtime_session_cleanup_status
            == RuntimeSessionCleanupStatus.SUCCEEDED
        )


async def test_diagnostic_db_outage_has_deadline_and_safe_rate_limited_log_fallback(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    observation = await _observation(execution_service, engine)
    recorder = BackgroundDiagnosticRecorder(create_session_factory(engine))

    async def stuck(*_args: Any, **_kwargs: Any) -> None:
        await asyncio.sleep(20)

    monkeypatch.setattr(RuntimeObservation, "current", stuck)
    async with asyncio.timeout(3):
        assert not await recorder.record(
            observation,
            PermissionError(errno.EACCES, "private-secret"),
            phase="MULTI_SESSION_PROBE",
            category=DiagnosticCategory.EXECUTION,
        )
    count = len(caplog.records)
    assert not await recorder.record(
        observation,
        PermissionError(errno.EACCES, "private-secret"),
        phase="MULTI_SESSION_PROBE",
        category=DiagnosticCategory.EXECUTION,
    )
    assert len(caplog.records) == count
    assert "DIAGNOSTIC_PERSIST" in caplog.text and "errno=13" in caplog.text
    assert "private-secret" not in caplog.text


async def test_observation_state_change_during_remote_delete_drops_old_result(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation = await _observation(execution_service, engine, terminal=True)
    factory = create_session_factory(engine)
    _driver(monkeypatch)

    async def delete(_self: Any, _session_id: str) -> None:
        async with factory() as session, session.begin():
            await session.execute(
                update(ExecutionORM)
                .where(ExecutionORM.id == observation.execution_id)
                .values(
                    version=observation.version + 1,
                    fencing_token=2,
                    runtime_session_id="new-session",
                    status=ExecutionStatus.RUNNING,
                )
            )
        raise RuntimeDriverError("late delete failure")

    monkeypatch.setattr(BackgroundDriver, "delete_session", delete)
    worker, redis = _worker(engine, tmp_path)
    try:
        await worker._session_recovery.cleanup(observation)
    finally:
        await redis.aclose()
    assert not (
        await SQLAlchemyDiagnosticQueryService(factory).list(
            observation.execution_id
        )
    ).items
    async with factory() as session:
        row = await session.get(ExecutionORM, observation.execution_id)
        assert row is not None and row.runtime_session_id == "new-session"
        assert (
            row.runtime_session_cleanup_status
            == RuntimeSessionCleanupStatus.PENDING
        )


async def test_stale_missing_session_probe_cannot_fail_new_operation(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation = await _observation(execution_service, engine)
    factory = create_session_factory(engine)
    _driver(monkeypatch)

    async def probe(_self: Any, _session_id: str) -> bool:
        async with factory() as session, session.begin():
            await session.execute(
                update(ExecutionORM)
                .where(
                    ExecutionORM.id == observation.execution_id,
                )
                .values(version=observation.version + 1, fencing_token=2)
            )
        return False

    monkeypatch.setattr(BackgroundDriver, "session_exists", probe)
    worker, redis = _worker(engine, tmp_path)
    try:
        await worker._multi_lifecycle.audit()
    finally:
        await redis.aclose()
    async with factory() as session:
        row = await session.get(ExecutionORM, observation.execution_id)
        assert (
            row is not None
            and row.status == ExecutionStatus.WAITING_FOR_OPERATION
        )
        assert row.runtime_session_id == observation.session_id
        assert row.failure_type is None


async def test_cleanup_state_persist_error_keeps_reservation_and_records_reason(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation = await _observation(execution_service, engine, terminal=True)
    factory = create_session_factory(engine)
    _driver(monkeypatch)
    worker, redis = _worker(engine, tmp_path)

    async def persist(*_args: Any, **_kwargs: Any) -> None:
        raise OSError(errno.EIO, "private")

    monkeypatch.setattr(worker._session_recovery, "record_result", persist)
    try:
        await worker._session_recovery.cleanup(observation)
    finally:
        await redis.aclose()
    items = (
        await SQLAlchemyDiagnosticQueryService(factory).list(
            observation.execution_id
        )
    ).items
    assert items[0].diagnostic.phase == "RECOVERY_RESULT_PERSIST"
    assert items[0].diagnostic.code == "OS_ERROR"
    async with factory() as session:
        row = await observation.current(session)
        assert row is not None
        assert (
            row.runtime_session_cleanup_status
            == RuntimeSessionCleanupStatus.PENDING
        )


async def test_one_probe_factory_failure_does_not_skip_other_waiting_deadlines(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = await _observation(execution_service, engine)
    second = await _observation(execution_service, engine)
    factory = create_session_factory(engine)
    async with factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == second.execution_id)
            .values(
                operation_wait_expires_at=utc_now() - timedelta(seconds=1),
            )
        )
    _driver(monkeypatch, "create")
    worker, redis = _worker(engine, tmp_path)
    try:
        await worker._multi_lifecycle.audit()
    finally:
        await redis.aclose()
    async with factory() as session:
        first_row = await session.get(ExecutionORM, first.execution_id)
        second_row = await session.get(ExecutionORM, second.execution_id)
        assert (
            first_row is not None
            and first_row.status == ExecutionStatus.WAITING_FOR_OPERATION
        )
        assert (
            second_row is not None
            and second_row.failure_type == FailureType.OPERATION_WAIT_TIMEOUT
        )
        assert (
            second_row.runtime_session_cleanup_status
            == RuntimeSessionCleanupStatus.FAILED
        )


def test_unscoped_loop_logs_are_safe_and_bounded(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    recorder = BackgroundDiagnosticRecorder(create_session_factory(engine))
    for _ in range(10):
        recorder.log_loop_failure(
            ValueError("SQL parameters password=do-not-log"),
            phase="RETAINED_CLEANUP_SCAN",
        )
    assert len(caplog.records) == 1
    assert "RETAINED_CLEANUP_SCAN" in caplog.text
    assert "do-not-log" not in caplog.text
    assert "SQL parameters" not in caplog.text
    for index in range(1100):
        recorder._remember((index,))
    assert len(recorder._recent) == 1024
