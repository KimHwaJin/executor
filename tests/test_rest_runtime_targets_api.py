from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest_asyncio
from sqlalchemy import select, update

from executor_service.config import Settings
from executor_service.container import ApplicationContainer
from executor_service.domain.runtime import RuntimeResourceMetric, RuntimeResourceObservation
from executor_service.infrastructure.db.base import Base
from executor_service.infrastructure.db.models import ExecutionORM, RuntimeTargetPurgeORM
from executor_service.interfaces.http.app import create_app


class HealthyGateway:
    fail_resource_probe = False

    def __init__(self, _endpoint: str, _token: str, _timeout: float) -> None:
        pass

    async def status(self) -> dict[str, int]:
        return {"active_session_count": 1}

    async def supported_profiles(self) -> list[str]:
        return ["basic", "ml"]

    async def resource_status(self) -> RuntimeResourceObservation:
        if self.fail_resource_probe:
            raise RuntimeError("resource endpoint unavailable")
        now = datetime.now(UTC)
        return RuntimeResourceObservation(
            observed_at=now,
            process_count=3,
            cpu=RuntimeResourceMetric(0.4, 2.0, 0.2, "CGROUP_V2", False),
            memory=RuntimeResourceMetric(256, 1024, 0.25, "CGROUP_V2", False),
        )

    async def close(self) -> None:
        pass


@pytest_asyncio.fixture
async def fleet_client(
    tmp_path: Path, monkeypatch: Any
) -> AsyncIterator[tuple[httpx.AsyncClient, ApplicationContainer]]:
    HealthyGateway.fail_resource_probe = False
    monkeypatch.setattr(
        "executor_service.infrastructure.runtime_drivers.JupyterRuntimeDriver",
        HealthyGateway,
    )
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        redis_url="redis://localhost:6399/15",
        runtime_enabled=False,
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
        "name": name or f"fleet-target-{index}",
        "runtime_type": "JUPYTER",
        "connection_config": {"endpoint": f"http://jupyter-{index}:8888"},
        "credential": f"never-return-this-secret-{index}",
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
        "runtime_profile": "basic",
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
            "user_id": "fleet-admin",
            "project_id": "fleet-project",
            "session_id": "fleet-session",
            "task_id": "fleet-task",
        },
        "actor": {"type": "USER", "id": "fleet-admin"},
    }


async def test_openapi_documents_runtime_fleet_routes_and_never_returns_token(
    fleet_client: tuple[httpx.AsyncClient, ApplicationContainer],
) -> None:
    client, _ = fleet_client
    openapi = await client.get("/openapi.json")
    paths = openapi.json()["paths"]
    assert {
        "/api/v1/runtime-targets",
        "/api/v1/runtime-pools",
        "/api/v1/runtime-targets/{target_id}",
        "/api/v1/runtime-targets/{target_id}/probe",
        "/api/v1/runtime-targets/{target_id}/drain",
        "/api/v1/runtime-targets/{target_id}/activate",
        "/api/v1/runtime-targets/{target_id}/disable",
        "/api/v1/runtime-targets/{target_id}/purge",
    } <= set(paths)

    payload = _upsert_payload(0)
    created = await client.post("/api/v1/runtime-targets", json=payload)
    assert created.status_code == 200
    assert payload["credential"] not in created.text
    assert "credential" not in created.json()
    assert created.json()["state"]["status"] == "ACTIVE"
    assert created.json()["created_by"] == "fleet-admin"
    assert created.json()["updated_by"] == "fleet-admin"
    assert created.json()["runtime"]["supported_profiles"] == ["basic", "ml"]
    assert created.json()["capacity"]["available_capacity"] == 2
    assert created.json()["resources"]["fresh"] is True
    assert created.json()["resources"]["pressure_score"] == 0.25
    assert created.json()["resources"]["memory"]["utilization"] == 0.25


async def test_resource_only_probe_failure_keeps_target_active_and_marks_data_stale(
    fleet_client: tuple[httpx.AsyncClient, ApplicationContainer],
) -> None:
    client, _ = fleet_client
    created = await client.post("/api/v1/runtime-targets", json=_upsert_payload(20))
    target_id = created.json()["target_id"]
    HealthyGateway.fail_resource_probe = True
    try:
        probed = await client.post(
            f"/api/v1/runtime-targets/{target_id}/probe",
            json={"actor": {"type": "USER", "id": "fleet-admin"}},
        )
    finally:
        HealthyGateway.fail_resource_probe = False

    assert probed.status_code == 200
    assert probed.json()["state"]["status"] == "ACTIVE"
    assert probed.json()["resources"]["fresh"] is False
    assert probed.json()["resources"]["last_error"] == "Resource probe failed (RuntimeError)"
    assert probed.json()["resources"]["memory"]["utilization"] == 0.25


async def test_fleet_list_filters_cursor_capacity_and_state_controls(
    fleet_client: tuple[httpx.AsyncClient, ApplicationContainer],
) -> None:
    client, _ = fleet_client
    target_ids: list[str] = []
    for index in range(3):
        response = await client.post(
            "/api/v1/runtime-targets",
            json=_upsert_payload(index, pool="BATCH" if index == 2 else "INTERACTIVE"),
        )
        target_ids.append(response.json()["target_id"])

    first = await client.get("/api/v1/runtime-targets", params={"limit": 2})
    assert first.status_code == 200
    assert len(first.json()["items"]) == 2
    assert first.json()["has_more"] is True
    second = await client.get(
        "/api/v1/runtime-targets",
        params={"limit": 2, "cursor": first.json()["next_cursor"]},
    )
    assert len(second.json()["items"]) == 1
    assert second.json()["has_more"] is False

    batch = await client.get(
        "/api/v1/runtime-targets",
        params={"pool": "BATCH", "status": "ACTIVE", "enabled": True},
    )
    assert [item["target_id"] for item in batch.json()["items"]] == [target_ids[2]]

    pools = await client.get("/api/v1/runtime-pools")
    summaries = {
        (item["runtime"]["type"], item["runtime"]["pool"]): item
        for item in pools.json()["items"]
    }
    assert summaries[("JUPYTER", "INTERACTIVE")]["targets"]["total"] == 2
    assert summaries[("JUPYTER", "INTERACTIVE")]["capacity"]["configured"] == 5
    assert summaries[("JUPYTER", "INTERACTIVE")]["capacity"]["available"] == 5
    assert summaries[("JUPYTER", "INTERACTIVE")]["state"]["accepting_new_executions"] is True
    assert summaries[("JUPYTER", "BATCH")]["targets"]["total"] == 1

    target_id = target_ids[0]
    drained = await client.post(
        f"/api/v1/runtime-targets/{target_id}/drain",
        json=_mutation_payload("fleet-drain-0"),
    )
    assert drained.json()["state"]["status"] == "DRAINING"
    assert drained.json()["capacity"]["available_capacity"] == 0

    activated = await client.post(
        f"/api/v1/runtime-targets/{target_id}/activate",
        json=_mutation_payload("fleet-activate-0"),
    )
    assert activated.json()["state"]["status"] == "ACTIVE"
    assert activated.json()["state"]["accepting_new_executions"] is True

    disabled = await client.post(
        f"/api/v1/runtime-targets/{target_id}/disable",
        json=_mutation_payload("fleet-disable-0"),
    )
    assert disabled.json()["state"]["status"] == "OFFLINE"
    assert disabled.json()["state"]["enabled"] is False
    filtered = await client.get("/api/v1/runtime-targets", params={"enabled": False})
    assert [item["target_id"] for item in filtered.json()["items"]] == [target_id]


async def test_hard_purge_requires_disable_confirmation_and_keeps_tombstone(
    fleet_client: tuple[httpx.AsyncClient, ApplicationContainer],
) -> None:
    client, container = fleet_client
    created = await client.post("/api/v1/runtime-targets", json=_upsert_payload(10))
    target_id = created.json()["target_id"]
    purge_payload = {
        **_mutation_payload("fleet-purge-10"),
        "confirmation_name": "fleet-target-10",
    }

    active_purge = await client.post(
        f"/api/v1/runtime-targets/{target_id}/purge", json=purge_payload
    )
    assert active_purge.status_code == 409
    assert active_purge.json()["error"]["code"] == "RUNTIME_TARGET_PURGE_CONFLICT"

    await client.post(
        f"/api/v1/runtime-targets/{target_id}/disable",
        json=_mutation_payload("fleet-disable-10"),
    )
    wrong_name = await client.post(
        f"/api/v1/runtime-targets/{target_id}/purge",
        json={**purge_payload, "confirmation_name": "wrong-name"},
    )
    assert wrong_name.status_code == 409

    purged = await client.post(f"/api/v1/runtime-targets/{target_id}/purge", json=purge_payload)
    repeated = await client.post(f"/api/v1/runtime-targets/{target_id}/purge", json=purge_payload)
    assert purged.status_code == 200
    assert repeated.json() == purged.json()
    assert purged.json()["created_by_type"] == "USER"
    assert purged.json()["created_by"] == "fleet-admin"
    assert purged.json()["updated_by_type"] == "USER"
    assert purged.json()["updated_by"] == "fleet-admin"
    assert purged.json()["created_at"] is not None
    assert purged.json()["updated_at"] is not None
    assert (await client.get(f"/api/v1/runtime-targets/{target_id}")).status_code == 404

    async with container.session_factory() as session:
        tombstone = await session.scalar(
            select(RuntimeTargetPurgeORM).where(RuntimeTargetPurgeORM.target_id == UUID(target_id))
        )
        assert tombstone is not None
        assert tombstone.target_name == "fleet-target-10"
        assert tombstone.created_by == "fleet-admin"


async def test_environment_configured_default_server_cannot_be_purged(
    fleet_client: tuple[httpx.AsyncClient, ApplicationContainer],
) -> None:
    client, container = fleet_client
    container.settings.runtime_enabled = True
    created = await client.post(
        "/api/v1/runtime-targets",
        json=_upsert_payload(20, name=container.settings.runtime_target_name),
    )
    target_id = created.json()["target_id"]
    await client.post(
        f"/api/v1/runtime-targets/{target_id}/disable",
        json=_mutation_payload("fleet-disable-default"),
    )
    purged = await client.post(
        f"/api/v1/runtime-targets/{target_id}/purge",
        json={
            **_mutation_payload("fleet-purge-default"),
            "confirmation_name": container.settings.runtime_target_name,
        },
    )
    assert purged.status_code == 409
    assert "environment-configured" in purged.json()["error"]["message"]


async def test_server_referenced_by_execution_history_cannot_be_purged(
    fleet_client: tuple[httpx.AsyncClient, ApplicationContainer],
) -> None:
    client, container = fleet_client
    created = await client.post("/api/v1/runtime-targets", json=_upsert_payload(30))
    target_id = UUID(created.json()["target_id"])
    execution = await client.post("/api/v1/executions", json=_execution_payload())
    execution_id = UUID(execution.json()["execution_id"])
    async with container.session_factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution_id)
            .values(runtime_target_id=target_id)
        )

    await client.post(
        f"/api/v1/runtime-targets/{target_id}/disable",
        json=_mutation_payload("fleet-disable-history"),
    )
    purged = await client.post(
        f"/api/v1/runtime-targets/{target_id}/purge",
        json={
            **_mutation_payload("fleet-purge-history"),
            "confirmation_name": "fleet-target-30",
        },
    )
    assert purged.status_code == 409
    assert "Execution or Attempt history" in purged.json()["error"]["message"]
