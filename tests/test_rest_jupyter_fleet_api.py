from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest_asyncio
from sqlalchemy import select, update

from executor_service.config import Settings
from executor_service.container import ApplicationContainer
from executor_service.infrastructure.db.base import Base
from executor_service.infrastructure.db.models import ExecutionORM, JupyterServerPurgeORM
from executor_service.interfaces.http.app import create_app


class HealthyGateway:
    def __init__(self, _endpoint: str, _token: str, _timeout: float) -> None:
        pass

    async def status(self) -> dict[str, int]:
        return {"kernels": 1}

    async def kernel_specs(self) -> list[str]:
        return ["python3", "analytics"]

    async def close(self) -> None:
        pass


@pytest_asyncio.fixture
async def fleet_client(
    tmp_path: Path, monkeypatch: Any
) -> AsyncIterator[tuple[httpx.AsyncClient, ApplicationContainer]]:
    monkeypatch.setattr(
        "executor_service.infrastructure.jupyter_registry.JupyterGateway",
        HealthyGateway,
    )
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        redis_url="redis://localhost:6399/15",
        jupyter_enabled=False,
        workspace_host_root=tmp_path,
    )
    container = ApplicationContainer(settings)
    async with container.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    app = create_app(container)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client, container
    await container.redis.aclose()
    await container.engine.dispose()


def _upsert_payload(
    index: int,
    *,
    pool: str = "INTERACTIVE",
    name: str | None = None,
) -> dict[str, Any]:
    return {
        "idempotency_key": f"fleet-upsert-{index}",
        "name": name or f"fleet-server-{index}",
        "endpoint": f"http://jupyter-{index}:8888",
        "token": f"never-return-this-secret-{index}",
        "pool": pool,
        "max_concurrent_executions": index + 2,
        "actor": {"type": "USER", "id": "fleet-admin"},
    }


def _mutation_payload(key: str) -> dict[str, Any]:
    return {
        "idempotency_key": key,
        "actor": {"type": "USER", "id": "fleet-admin"},
    }


def _execution_payload() -> dict[str, Any]:
    return {
        "idempotency_key": "fleet-history-execution",
        "mode": "STATIC",
        "trigger_type": "INTERACTIVE",
        "kernel_name": "python3",
        "source": {
            "type": "INLINE",
            "spec": {
                "schema_version": "1.0",
                "execution_plan_id": "fleet-history-plan",
                "steps": [
                    {
                        "sequence": 0,
                        "plan_step_id": "fleet-history-step",
                        "code": "print('history')",
                    }
                ],
            },
        },
        "context": {
            "requested_by_user_id": "fleet-admin",
            "project_id": "fleet-project",
            "session_id": "fleet-session",
            "task_id": "fleet-task",
        },
        "actor": {"type": "USER", "id": "fleet-admin"},
    }


async def test_openapi_documents_jupyter_fleet_routes_and_never_returns_token(
    fleet_client: tuple[httpx.AsyncClient, ApplicationContainer],
) -> None:
    client, _ = fleet_client
    openapi = await client.get("/openapi.json")
    paths = openapi.json()["paths"]
    assert {
        "/api/v1/jupyter-servers",
        "/api/v1/jupyter-pools",
        "/api/v1/jupyter-servers/{server_id}",
        "/api/v1/jupyter-servers/{server_id}/probe",
        "/api/v1/jupyter-servers/{server_id}/drain",
        "/api/v1/jupyter-servers/{server_id}/activate",
        "/api/v1/jupyter-servers/{server_id}/purge",
    } <= set(paths)

    payload = _upsert_payload(0)
    created = await client.post("/api/v1/jupyter-servers", json=payload)
    assert created.status_code == 200
    assert payload["token"] not in created.text
    assert "token" not in created.json()
    assert created.json()["status"] == "ACTIVE"
    assert created.json()["created_by"] == "fleet-admin"
    assert created.json()["updated_by"] == "fleet-admin"
    assert created.json()["available_capacity"] == 2


async def test_fleet_list_filters_cursor_capacity_and_state_controls(
    fleet_client: tuple[httpx.AsyncClient, ApplicationContainer],
) -> None:
    client, _ = fleet_client
    server_ids: list[str] = []
    for index in range(3):
        response = await client.post(
            "/api/v1/jupyter-servers",
            json=_upsert_payload(index, pool="BATCH" if index == 2 else "INTERACTIVE"),
        )
        server_ids.append(response.json()["server_id"])

    first = await client.get("/api/v1/jupyter-servers", params={"limit": 2})
    assert first.status_code == 200
    assert len(first.json()["items"]) == 2
    assert first.json()["has_more"] is True
    second = await client.get(
        "/api/v1/jupyter-servers",
        params={"limit": 2, "cursor": first.json()["next_cursor"]},
    )
    assert len(second.json()["items"]) == 1
    assert second.json()["has_more"] is False

    batch = await client.get(
        "/api/v1/jupyter-servers",
        params={"pool": "BATCH", "status": "ACTIVE", "enabled": True},
    )
    assert [item["server_id"] for item in batch.json()["items"]] == [server_ids[2]]

    pools = await client.get("/api/v1/jupyter-pools")
    summaries = {item["pool"]: item for item in pools.json()["items"]}
    assert summaries["INTERACTIVE"]["server_count"] == 2
    assert summaries["INTERACTIVE"]["configured_capacity"] == 5
    assert summaries["INTERACTIVE"]["available_capacity"] == 5
    assert summaries["INTERACTIVE"]["accepting_new_executions"] is True
    assert summaries["BATCH"]["server_count"] == 1

    server_id = server_ids[0]
    drained = await client.post(
        f"/api/v1/jupyter-servers/{server_id}/drain",
        json=_mutation_payload("fleet-drain-0"),
    )
    assert drained.json()["status"] == "DRAINING"
    assert drained.json()["available_capacity"] == 0

    activated = await client.post(
        f"/api/v1/jupyter-servers/{server_id}/activate",
        json=_mutation_payload("fleet-activate-0"),
    )
    assert activated.json()["status"] == "ACTIVE"
    assert activated.json()["accepting_new_executions"] is True

    removed = await client.request(
        "DELETE",
        f"/api/v1/jupyter-servers/{server_id}",
        json=_mutation_payload("fleet-remove-0"),
    )
    assert removed.json()["status"] == "OFFLINE"
    assert removed.json()["enabled"] is False
    filtered = await client.get(
        "/api/v1/jupyter-servers", params={"enabled": False}
    )
    assert [item["server_id"] for item in filtered.json()["items"]] == [server_id]


async def test_hard_purge_requires_soft_delete_confirmation_and_keeps_tombstone(
    fleet_client: tuple[httpx.AsyncClient, ApplicationContainer],
) -> None:
    client, container = fleet_client
    created = await client.post("/api/v1/jupyter-servers", json=_upsert_payload(10))
    server_id = created.json()["server_id"]
    purge_payload = {
        **_mutation_payload("fleet-purge-10"),
        "confirmation_name": "fleet-server-10",
    }

    active_purge = await client.post(
        f"/api/v1/jupyter-servers/{server_id}/purge", json=purge_payload
    )
    assert active_purge.status_code == 409
    assert active_purge.json()["error"]["code"] == "JupyterServerPurgeConflictError"

    await client.request(
        "DELETE",
        f"/api/v1/jupyter-servers/{server_id}",
        json=_mutation_payload("fleet-remove-10"),
    )
    wrong_name = await client.post(
        f"/api/v1/jupyter-servers/{server_id}/purge",
        json={**purge_payload, "confirmation_name": "wrong-name"},
    )
    assert wrong_name.status_code == 409

    purged = await client.post(
        f"/api/v1/jupyter-servers/{server_id}/purge", json=purge_payload
    )
    repeated = await client.post(
        f"/api/v1/jupyter-servers/{server_id}/purge", json=purge_payload
    )
    assert purged.status_code == 200
    assert repeated.json() == purged.json()
    assert purged.json()["purged_by"] == "fleet-admin"
    assert (await client.get(f"/api/v1/jupyter-servers/{server_id}")).status_code == 404

    async with container.session_factory() as session:
        tombstone = await session.scalar(
            select(JupyterServerPurgeORM).where(
                JupyterServerPurgeORM.server_id == UUID(server_id)
            )
        )
        assert tombstone is not None
        assert tombstone.server_name == "fleet-server-10"
        assert tombstone.created_by == "fleet-admin"


async def test_environment_configured_default_server_cannot_be_purged(
    fleet_client: tuple[httpx.AsyncClient, ApplicationContainer],
) -> None:
    client, container = fleet_client
    container.settings.jupyter_enabled = True
    created = await client.post(
        "/api/v1/jupyter-servers",
        json=_upsert_payload(20, name=container.settings.jupyter_server_name),
    )
    server_id = created.json()["server_id"]
    await client.request(
        "DELETE",
        f"/api/v1/jupyter-servers/{server_id}",
        json=_mutation_payload("fleet-remove-default"),
    )
    purged = await client.post(
        f"/api/v1/jupyter-servers/{server_id}/purge",
        json={
            **_mutation_payload("fleet-purge-default"),
            "confirmation_name": container.settings.jupyter_server_name,
        },
    )
    assert purged.status_code == 409
    assert "environment-configured" in purged.json()["error"]["message"]


async def test_server_referenced_by_execution_history_cannot_be_purged(
    fleet_client: tuple[httpx.AsyncClient, ApplicationContainer],
) -> None:
    client, container = fleet_client
    created = await client.post("/api/v1/jupyter-servers", json=_upsert_payload(30))
    server_id = UUID(created.json()["server_id"])
    execution = await client.post("/api/v1/executions", json=_execution_payload())
    execution_id = UUID(execution.json()["execution_id"])
    async with container.session_factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution_id)
            .values(jupyter_server_id=server_id)
        )

    await client.request(
        "DELETE",
        f"/api/v1/jupyter-servers/{server_id}",
        json=_mutation_payload("fleet-remove-history"),
    )
    purged = await client.post(
        f"/api/v1/jupyter-servers/{server_id}/purge",
        json={
            **_mutation_payload("fleet-purge-history"),
            "confirmation_name": "fleet-server-30",
        },
    )
    assert purged.status_code == 409
    assert "Execution or Attempt history" in purged.json()["error"]["message"]
