from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest_asyncio

from executor_service.config import Settings
from executor_service.container import ApplicationContainer
from executor_service.domain.enums import RuntimePool, RuntimeTargetStatus
from executor_service.infrastructure.db.base import Base
from executor_service.infrastructure.db.models import RuntimeTargetORM
from executor_service.interfaces.http.app import create_app
from tests.runtime_credentials import runtime_credential_fields


@pytest_asyncio.fixture
async def maintenance_client(
    tmp_path: Path,
) -> AsyncIterator[tuple[httpx.AsyncClient, ApplicationContainer]]:
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        redis_url="redis://localhost:6399/15",
        runtime_enabled=False,
        shared_storage_root=tmp_path,
    )
    container = ApplicationContainer(settings)
    async with container.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await container.maintenance.initialize()
    app = create_app(container)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        yield client, container
    await container.redis.aclose()
    await container.engine.dispose()


def _mutation(key: str) -> dict[str, Any]:
    return {
        "idempotency_key": key,
        "actor": {"type": "USER", "id": "operator-1"},
    }


def _execution(key: str) -> dict[str, Any]:
    return {
        "idempotency_key": key,
        "lifecycle": {"operation_mode": "SINGLE"},
        "trigger": {
            "type": "INTERACTIVE",
            "actor": {"type": "USER", "id": "user-1"},
        },
        "runtime": {"type": "JUPYTER", "profile": "basic"},
        "operation": {
            "spec": {
                "schema_version": "1.0",
                "steps": [
                    {
                        "sequence": 0,
                        "payload": {
                            "type": "PYTHON_EXECUTE",
                            "source": {
                                "type": "INLINE",
                                "content": "print('queued')",
                            },
                        },
                    }
                ],
            }
        },
        "context": {"user_id": "user-1", "task_id": "task-1"},
    }


async def test_maintenance_defaults_to_active_and_is_safe(
    maintenance_client: tuple[httpx.AsyncClient, ApplicationContainer],
) -> None:
    client, _container = maintenance_client

    response = await client.get("/api/v1/maintenance")

    assert response.status_code == 200
    payload = response.json()
    assert payload["admission"] == {
        "state": "ACTIVE",
        "accepting_new_executions": True,
        "version": 0,
    }
    assert payload["workload"] == {
        "queued_execution_count": 0,
        "active_execution_count": 0,
        "cancel_requested_count": 0,
    }
    assert payload["cleanup"] == {
        "unresolved_cleanup_count": 0,
        "active_runtime_session_count": 0,
    }
    assert payload["safe_to_shutdown"] is True
    assert payload["created_by_type"] is None
    assert payload["updated_by_type"] is None


async def test_drain_is_persistent_idempotent_and_activate_resumes(
    maintenance_client: tuple[httpx.AsyncClient, ApplicationContainer],
) -> None:
    client, container = maintenance_client

    drained = await client.post(
        "/api/v1/maintenance/drain", json=_mutation("drain-1")
    )
    repeated = await client.post(
        "/api/v1/maintenance/drain", json=_mutation("drain-1")
    )

    assert drained.status_code == 200
    assert repeated.status_code == 200
    assert drained.json()["admission"] == {
        "state": "DRAINING",
        "accepting_new_executions": False,
        "version": 1,
    }
    assert repeated.json()["admission"] == drained.json()["admission"]
    assert drained.json()["updated_by"] == "operator-1"

    conflict = await client.post(
        "/api/v1/maintenance/activate", json=_mutation("drain-1")
    )
    assert conflict.status_code == 409

    restarted_service_view = await container.maintenance.get()
    assert restarted_service_view.admission_state == "DRAINING"

    activated = await client.post(
        "/api/v1/maintenance/activate", json=_mutation("activate-1")
    )
    assert activated.status_code == 200
    assert activated.json()["admission"] == {
        "state": "ACTIVE",
        "accepting_new_executions": True,
        "version": 2,
    }


async def test_drain_keeps_new_submissions_queued(
    maintenance_client: tuple[httpx.AsyncClient, ApplicationContainer],
) -> None:
    client, _container = maintenance_client
    await client.post(
        "/api/v1/maintenance/drain", json=_mutation("drain-for-queue")
    )

    submitted = await client.post(
        "/api/v1/executions", json=_execution("queued-during-drain")
    )
    status = await client.get("/api/v1/maintenance")

    assert submitted.status_code == 202
    assert status.json()["workload"]["queued_execution_count"] == 1
    assert status.json()["safe_to_shutdown"] is True


async def test_worker_claim_obeys_drain_and_activate(
    maintenance_client: tuple[httpx.AsyncClient, ApplicationContainer],
) -> None:
    client, container = maintenance_client
    async with container.session_factory() as session, session.begin():
        session.add(
            RuntimeTargetORM(
                name="maintenance-target",
                connection_config={"endpoint": "http://runtime.invalid:8888"},
                **runtime_credential_fields(),
                pool=RuntimePool.INTERACTIVE,
                status=RuntimeTargetStatus.ACTIVE,
                max_concurrent_executions=1,
                supported_profiles=["basic"],
                enabled=True,
            )
        )
    submitted = await client.post(
        "/api/v1/executions", json=_execution("claim-during-drain")
    )
    execution_id = UUID(submitted.json()["execution_id"])
    await client.post(
        "/api/v1/maintenance/drain", json=_mutation("claim-drain")
    )

    assert await container.execution_worker._claim(execution_id) is None

    await client.post(
        "/api/v1/maintenance/activate", json=_mutation("claim-activate")
    )
    claimed = await container.execution_worker._claim(execution_id)
    assert claimed is not None
