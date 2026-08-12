from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.runtime_targets import (
    RemoveRuntimeTargetCommand,
    SetRuntimeTargetStateCommand,
    UpsertRuntimeTargetCommand,
)
from executor_service.config import Settings
from executor_service.domain.enums import RuntimePool, RuntimeTargetStatus
from executor_service.domain.errors import (
    IdempotencyConflictError,
    RuntimeTargetConfigurationError,
)
from executor_service.infrastructure.db.models import RuntimeTargetORM
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.runtime_registry import RuntimeTargetRegistry


@pytest.mark.asyncio
async def test_registry_encrypts_credentials_and_soft_removes_idempotently(
    engine: AsyncEngine,
) -> None:
    settings = Settings(
        runtime_enabled=False,
        jupyter_request_timeout_seconds=0.1,
        runtime_credential_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    )
    session_factory = create_session_factory(engine)
    registry = RuntimeTargetRegistry(session_factory, settings)
    credential = f"secret-{uuid4()}"
    command = UpsertRuntimeTargetCommand(
        idempotency_key="register-secondary",
        name="secondary",
        connection_config={"endpoint": "http://127.0.0.1:9"},
        credential=credential,
        pool=RuntimePool.INTERACTIVE,
        max_concurrent_executions=3,
    )

    created = await registry.upsert(command)
    repeated = await registry.upsert(command)

    assert repeated.id == created.id
    assert created.status == RuntimeTargetStatus.OFFLINE
    assert created.enabled
    assert created.last_health_error == "Probe failed (RuntimeDriverError)"

    async with session_factory() as session:
        row = await session.scalar(
            select(RuntimeTargetORM).where(RuntimeTargetORM.id == created.id)
        )
        assert row is not None
        assert row.credential_ciphertext is not None
        assert credential not in row.credential_ciphertext
        assert (
            registry.resolve_credential(row.credential_ref, row.credential_ciphertext) == credential
        )

    removed = await registry.remove(
        RemoveRuntimeTargetCommand(idempotency_key="remove-secondary", target_id=created.id)
    )
    repeated_remove = await registry.remove(
        RemoveRuntimeTargetCommand(idempotency_key="remove-secondary", target_id=created.id)
    )
    assert not removed.enabled
    assert repeated_remove.id == removed.id
    assert repeated_remove.status == removed.status
    assert repeated_remove.enabled == removed.enabled

    draining = await registry.set_state(
        SetRuntimeTargetStateCommand(
            idempotency_key="drain-secondary",
            target_id=created.id,
            desired_state=RuntimeTargetStatus.DRAINING,
        )
    )
    assert draining.enabled
    assert draining.status == RuntimeTargetStatus.DRAINING
    assert draining.drain_complete
    assert not draining.accepting_new_executions


@pytest.mark.asyncio
async def test_registry_rejects_reused_key_with_different_request(
    engine: AsyncEngine,
) -> None:
    settings = Settings(
        runtime_enabled=False,
        jupyter_request_timeout_seconds=0.1,
        runtime_credential_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    )
    registry = RuntimeTargetRegistry(create_session_factory(engine), settings)
    await registry.upsert(
        UpsertRuntimeTargetCommand(
            idempotency_key="same-key",
            name="target-a",
            connection_config={"endpoint": "http://127.0.0.1:9"},
            credential="secret-a",
            pool=RuntimePool.INTERACTIVE,
        )
    )

    with pytest.raises(IdempotencyConflictError):
        await registry.upsert(
            UpsertRuntimeTargetCommand(
                idempotency_key="same-key",
                name="target-b",
                connection_config={"endpoint": "http://127.0.0.1:9"},
                credential="secret-b",
                pool=RuntimePool.INTERACTIVE,
            )
        )


@pytest.mark.asyncio
async def test_registry_rejects_unexpected_driver_connection_fields(
    engine: AsyncEngine,
) -> None:
    settings = Settings(
        runtime_enabled=False,
        runtime_credential_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    )
    registry = RuntimeTargetRegistry(create_session_factory(engine), settings)

    with pytest.raises(RuntimeTargetConfigurationError):
        await registry.upsert(
            UpsertRuntimeTargetCommand(
                idempotency_key="unsafe-connection-config",
                name="unsafe-target",
                connection_config={
                    "endpoint": "http://127.0.0.1:8888",
                    "token": "must-not-be-stored-here",
                },
                credential="secret",
                pool=RuntimePool.INTERACTIVE,
            )
        )
