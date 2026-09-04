"""Durable evidence, fence safety, pagination and transport contract checks."""

import asyncio
import errno
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.services import ExecutionService
from executor_service.container import ApplicationContainer
from executor_service.domain.diagnostics import DiagnosticCategory as Category
from executor_service.domain.enums import RuntimePool, RuntimeTargetStatus
from executor_service.domain.errors import InvalidCursorError
from executor_service.domain.models import utc_now
from executor_service.domain.runtime import (
    RuntimeDriver,
    RuntimeDriverError,
    RuntimeExecutionError,
)
from executor_service.infrastructure.db.models import (
    ExecutionORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.diagnostic_mapping import diagnostic_for
from executor_service.infrastructure.diagnostic_store import (
    DiagnosticRecorder,
    SQLAlchemyDiagnosticQueryService,
)
from executor_service.infrastructure.execution_leases import (
    CancellationLease,
)
from executor_service.infrastructure.execution_worker.step_executor import (
    ExecutionStepExecutor,
)
from executor_service.infrastructure.execution_worker.types import (
    StoredRuntimeExecutionError,
)
from executor_service.infrastructure.result_storage import (
    FilesystemExecutionResultStore,
)
from executor_service.interfaces.http.app import create_app
from executor_service.settings import Settings
from tests.runtime_credentials import runtime_credential_fields
from tests.test_multi_lifecycle import _multi_command, _worker
from tests.test_runtime_failure_evidence import EvidenceDriver


async def test_slow_diagnostics_does_not_replace_code_error_with_timeout(
    claimed, engine: AsyncEngine, tmp_path: Path, monkeypatch
):
    lease, execution = claimed
    factory = create_session_factory(engine)
    from executor_service.infrastructure.db.models import ExecutionStepORM

    async with factory() as session, session.begin():
        await session.execute(
            update(ExecutionStepORM)
            .where(ExecutionStepORM.execution_id == execution.id)
            .values(step_timeout_seconds=1)
        )
    executor = ExecutionStepExecutor(
        factory, FilesystemExecutionResultStore(tmp_path)
    )
    record = executor._diagnostics.record

    async def slow(*args, **kwargs):
        await asyncio.sleep(1.1)
        return await record(*args, **kwargs)

    monkeypatch.setattr(executor._diagnostics, "record", slow)
    step = execution.steps[0]
    with pytest.raises(
        StoredRuntimeExecutionError, match="primary tool failure"
    ):
        await executor.execute(
            cast(RuntimeDriver, EvidenceDriver("tool", False)),
            "kernel",
            step.code or "pass",
            execution.id,
            step.sequence,
            lease=lease,
            result_identity=executor.result_identity(step, lease),
            source_reference=executor.source_reference(step),
        )
    items = (
        await SQLAlchemyDiagnosticQueryService(factory).list(execution.id)
    ).items
    assert items[0].diagnostic.code == "CODE_EXECUTION_FAILED"


async def test_diagnostic_db_wait_has_a_deadline(
    claimed, engine, monkeypatch, caplog
):
    lease, _ = claimed

    async def stalled(*args, **kwargs):
        await asyncio.sleep(20)

    monkeypatch.setattr(
        "executor_service.infrastructure.diagnostic_store.require_active_lease",
        stalled,
    )
    recorder = DiagnosticRecorder(create_session_factory(engine))
    async with asyncio.timeout(3):
        assert not await recorder.record(
            lease,
            ValueError("private"),
            phase="EXECUTION_RUN",
            category=Category.EXECUTION,
        )
    assert "DIAGNOSTIC_PERSIST" in caplog.text


@pytest_asyncio.fixture
async def claimed(
    execution_service: ExecutionService, engine: AsyncEngine, tmp_path: Path
):
    factory = create_session_factory(engine)
    async with factory() as session, session.begin():
        session.add(
            RuntimeTargetORM(
                name="diagnostics-test",
                connection_config={"endpoint": "http://test"},
                **runtime_credential_fields(),
                supported_profiles=["basic"],
                max_concurrent_executions=10,
                pool=RuntimePool.INTERACTIVE,
                status=RuntimeTargetStatus.ACTIVE,
            )
        )
    execution = await execution_service.submit(_multi_command("diagnostics"))
    worker, redis = _worker(engine, tmp_path)
    try:
        claim = await worker._claimer.claim(execution.id)
        assert claim is not None
        yield claim[2], execution
    finally:
        await redis.aclose()


@pytest.mark.parametrize(
    "phase,category,origin",
    [
        ("RESULT_APPEND", Category.OUTPUT, "RESULT_STORAGE"),
        ("NOTEBOOK_BUILD", Category.NOTEBOOK, "EXECUTOR"),
        ("EXECUTION_RUN", Category.EXECUTION, "UNKNOWN"),
    ],
)
def test_safe_os_diagnostics(phase, category, origin):
    error = PermissionError(errno.EACCES, "password=secret", "/private/token")
    item = diagnostic_for(error, phase=phase, category=category)
    assert item.code == "PERMISSION_DENIED"
    assert item.origin == origin
    assert item.causes[0].errno == errno.EACCES
    assert "secret" not in item.message
    assert "/private" not in item.message


def test_bounded_causes_do_not_expose_user_code_or_secret_urls():
    error = RuntimeDriverError("https://user:secret@host/api?token=private")
    current = error
    for _ in range(10):
        child = RuntimeDriverError("x" * 5000 + "password=secret")
        current.__cause__ = child
        current = child
    item = diagnostic_for(
        error, phase="RUNTIME_EXECUTE", category=Category.EXECUTION
    )
    assert len(item.causes) == 8 and item.causes_truncated
    assert all(len(c.message) <= 2000 for c in item.causes)
    assert "secret" not in repr(item)
    code_error = diagnostic_for(
        RuntimeExecutionError("user secret", []),
        phase="RUNTIME_EXECUTE",
        category=Category.EXECUTION,
    )
    assert code_error.code == "CODE_EXECUTION_FAILED"
    assert "user secret" not in repr(code_error)


async def test_persist_query_paginate_and_fence(claimed, engine: AsyncEngine):
    lease, execution = claimed
    factory = create_session_factory(engine)
    recorder = DiagnosticRecorder(factory)
    reader = SQLAlchemyDiagnosticQueryService(factory)
    for phase in ("RESULT_APPEND", "RESULT_FAILURE_SAVE", "RESULT_FINALIZE"):
        assert await recorder.record(
            lease,
            PermissionError(errno.EACCES, "secret"),
            phase=phase,
            category=Category.OUTPUT,
            sequence=0,
        )
    page = await reader.list(execution.id, limit=2)
    assert len(page.items) == 2 and page.next_cursor
    tail = await reader.list(execution.id, limit=2, cursor=page.next_cursor)
    assert len(tail.items) == 1 and tail.next_cursor is None
    assert not (
        {item.id for item in page.items} & {item.id for item in tail.items}
    )
    assert all(
        item.attempt_id == lease.attempt_id
        and item.step_id == execution.steps[0].id
        for item in page.items
    )
    assert (
        len(
            (
                await reader.list(execution.id, step_id=execution.steps[0].id)
            ).items
        )
        == 3
    )
    assert not (await reader.list(execution.id, step_id=uuid4())).items
    with pytest.raises(InvalidCursorError):
        await reader.list(
            execution.id, attempt_id=lease.attempt_id, cursor=page.next_cursor
        )
    with pytest.raises(InvalidCursorError):
        await reader.list(uuid4(), cursor=page.next_cursor)
    before = (await reader.list(execution.id)).items
    for stale in (
        replace(lease, owner="other"),
        replace(lease, fencing_token=lease.fencing_token + 1),
        replace(lease, attempt_id=uuid4()),
    ):
        assert not await recorder.record(
            stale,
            ValueError("stale"),
            phase="EXECUTION_RUN",
            category=Category.EXECUTION,
        )
    async with factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution.id)
            .values(lease_expires_at=utc_now() - timedelta(seconds=1))
        )
    assert not await recorder.record(
        lease,
        ValueError("expired"),
        phase="EXECUTION_RUN",
        category=Category.EXECUTION,
    )
    assert (await reader.list(execution.id)).items == before


async def test_diagnostic_storage_failure_is_bounded_and_logged(
    claimed, engine: AsyncEngine, monkeypatch, caplog
):
    lease, _ = claimed

    def broken_factory():
        raise ConnectionError("database-url-with-secret")

    recorder = DiagnosticRecorder(create_session_factory(engine))
    monkeypatch.setattr(recorder, "_factory", broken_factory)
    assert not await recorder.record(
        lease,
        PermissionError(errno.EACCES, "root failure"),
        phase="RESULT_APPEND",
        category=Category.OUTPUT,
    )
    assert "DIAGNOSTIC_PERSIST" in caplog.text
    assert "errno=13" in caplog.text
    assert "database-url-with-secret" not in caplog.text


async def test_cancellation_fence_can_record_cleanup(
    claimed, engine: AsyncEngine
):
    lease, execution = claimed
    factory = create_session_factory(engine)
    cancel = CancellationLease(
        execution.id, "cancel-owner", lease.fencing_token + 1
    )
    async with factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution.id)
            .values(
                status="CANCEL_REQUESTED",
                fencing_token=cancel.fencing_token,
                cancellation_lease_owner=cancel.owner,
                cancellation_lease_expires_at=utc_now()
                + timedelta(seconds=30),
            )
        )
    recorder = DiagnosticRecorder(factory)
    assert await recorder.record(
        cancel,
        RuntimeDriverError("delete unavailable"),
        phase="RUNTIME_DELETE",
        category=Category.CLEANUP,
    )
    item = (
        await SQLAlchemyDiagnosticQueryService(factory).list(execution.id)
    ).items[0]
    assert (
        item.attempt_id is None and item.fencing_token == cancel.fencing_token
    )
    assert item.diagnostic.category == Category.CLEANUP
    assert not await recorder.record(
        lease,
        ValueError("old"),
        phase="RESULT_APPEND",
        category=Category.OUTPUT,
    )


async def test_rest_diagnostic_history_contract(
    claimed, engine: AsyncEngine, tmp_path: Path
):
    lease, execution = claimed
    factory = create_session_factory(engine)
    await DiagnosticRecorder(factory).record(
        lease,
        PermissionError(errno.EACCES, "private"),
        phase="RESULT_APPEND",
        category=Category.OUTPUT,
        sequence=0,
    )
    container = ApplicationContainer(
        Settings(
            database_url="sqlite+aiosqlite:///:memory:",
            shared_storage_root=tmp_path,
        )
    )
    container.diagnostic_queries = SQLAlchemyDiagnosticQueryService(factory)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(container)),
            base_url="http://testserver",
        ) as client:
            path = f"/api/v1/executions/{execution.id}/diagnostics"
            result = await client.get(path)
            assert result.status_code == 200
            body = result.json()
            assert body["has_more"] is False
            item = body["items"][0]
            assert item["diagnostic"]["code"] == "PERMISSION_DENIED"
            assert item["diagnostic"]["causes"][0]["errno"] == 13
            assert {
                "created_at",
                "created_by",
                "updated_at",
                "updated_by",
            } <= item.keys()
            assert (
                await client.get(path, params={"limit": 201})
            ).status_code == 422
            assert (
                await client.get(path, params={"cursor": "bad"})
            ).status_code == 422
            assert (
                await client.get(f"/api/v1/executions/{uuid4()}/diagnostics")
            ).status_code == 404
    finally:
        await container.redis.aclose()
        await container.engine.dispose()
