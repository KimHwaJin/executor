from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.jupyter_servers import (
    RemoveJupyterServerCommand,
    SetJupyterServerStateCommand,
    UpsertJupyterServerCommand,
)
from executor_service.config import Settings
from executor_service.domain.enums import JupyterPool, JupyterServerStatus
from executor_service.domain.errors import IdempotencyConflictError
from executor_service.infrastructure.db.models import JupyterServerORM
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.jupyter_registry import JupyterServerRegistry


@pytest.mark.asyncio
async def test_registry_encrypts_credentials_and_soft_removes_idempotently(
    engine: AsyncEngine,
) -> None:
    settings = Settings(
        jupyter_enabled=False,
        jupyter_request_timeout_seconds=0.1,
        jupyter_credential_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    )
    session_factory = create_session_factory(engine)
    registry = JupyterServerRegistry(session_factory, settings)
    token = f"secret-{uuid4()}"
    command = UpsertJupyterServerCommand(
        idempotency_key="register-secondary",
        name="secondary",
        endpoint="http://127.0.0.1:9",
        token=token,
        pool=JupyterPool.INTERACTIVE,
        max_concurrent_executions=3,
    )

    created = await registry.upsert(command)
    repeated = await registry.upsert(command)

    assert repeated.id == created.id
    assert created.status == JupyterServerStatus.OFFLINE
    assert created.enabled
    assert created.last_health_error == "Probe failed (JupyterGatewayError)"

    async with session_factory() as session:
        row = await session.scalar(
            select(JupyterServerORM).where(JupyterServerORM.id == created.id)
        )
        assert row is not None
        assert row.credential_ciphertext is not None
        assert token not in row.credential_ciphertext
        assert registry.resolve_token(row.credential_ref, row.credential_ciphertext) == token

    removed = await registry.remove(
        RemoveJupyterServerCommand(
            idempotency_key="remove-secondary", server_id=created.id
        )
    )
    repeated_remove = await registry.remove(
        RemoveJupyterServerCommand(
            idempotency_key="remove-secondary", server_id=created.id
        )
    )
    assert not removed.enabled
    assert repeated_remove.id == removed.id
    assert repeated_remove.status == removed.status
    assert repeated_remove.enabled == removed.enabled

    draining = await registry.set_state(
        SetJupyterServerStateCommand(
            idempotency_key="drain-secondary",
            server_id=created.id,
            desired_state=JupyterServerStatus.DRAINING,
        )
    )
    assert draining.enabled
    assert draining.status == JupyterServerStatus.DRAINING
    assert draining.drain_complete
    assert not draining.accepting_new_executions


@pytest.mark.asyncio
async def test_registry_rejects_reused_key_with_different_request(
    engine: AsyncEngine,
) -> None:
    settings = Settings(
        jupyter_enabled=False,
        jupyter_request_timeout_seconds=0.1,
        jupyter_credential_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    )
    registry = JupyterServerRegistry(create_session_factory(engine), settings)
    await registry.upsert(
        UpsertJupyterServerCommand(
            idempotency_key="same-key",
            name="server-a",
            endpoint="http://127.0.0.1:9",
            token="secret-a",
            pool=JupyterPool.INTERACTIVE,
        )
    )

    with pytest.raises(IdempotencyConflictError):
        await registry.upsert(
            UpsertJupyterServerCommand(
                idempotency_key="same-key",
                name="server-b",
                endpoint="http://127.0.0.1:9",
                token="secret-b",
                pool=JupyterPool.INTERACTIVE,
            )
        )
