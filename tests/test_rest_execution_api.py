import hashlib
import json
from collections.abc import AsyncIterator
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest_asyncio
from sqlalchemy import update

from executor_service.config import Settings
from executor_service.container import ApplicationContainer
from executor_service.domain.enums import (
    ExecutionStatus,
    RetryStrategy,
    StepStatus,
)
from executor_service.infrastructure.db.base import Base
from executor_service.infrastructure.db.models import ExecutionORM, ExecutionStepORM
from executor_service.interfaces.http.app import create_app


@pytest_asyncio.fixture
async def rest_client(
    tmp_path: Path,
) -> AsyncIterator[tuple[httpx.AsyncClient, ApplicationContainer]]:
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


def _submit_payload(
    *,
    key: str = "rest-submit-1",
    mode: str = "STATIC",
    plan_id: str = "plan-rest-1",
) -> dict[str, Any]:
    return {
        "idempotency_key": key,
        "mode": mode,
        "trigger_type": "INTERACTIVE",
        "runtime_profile": "python3",
        "source": {
            "type": "INLINE",
            "spec": {
                "schema_version": "1.0",
                "execution_plan_id": plan_id,
                "steps": [
                    {
                        "sequence": 0,
                        "plan_step_id": f"{plan_id}-step-0",
                        "skill_name": "data_load",
                        "tool_name": "load_data",
                        "input_parameters": {"product": "A"},
                        "code": "print('hello from REST')",
                    }
                ],
            },
        },
        "context": {
            "requested_by_user_id": "rest-user",
            "project_id": "rest-project",
            "session_id": "rest-session",
            "task_id": "rest-task",
        },
        "actor": {"type": "USER", "id": "rest-user"},
        "metadata": {"caller": "integration-test"},
    }


async def test_openapi_documents_all_execution_routes(
    rest_client: tuple[httpx.AsyncClient, ApplicationContainer],
) -> None:
    client, _ = rest_client
    docs = await client.get("/docs")
    openapi = await client.get("/openapi.json")

    assert docs.status_code == 200
    assert openapi.status_code == 200
    paths = openapi.json()["paths"]
    assert {
        "/api/v1/capabilities",
        "/api/v1/executions",
        "/api/v1/executions/{execution_id}",
        "/api/v1/executions/{execution_id}/cancel",
        "/api/v1/executions/{execution_id}/retry",
        "/api/v1/executions/{execution_id}/continue",
        "/api/v1/executions/{execution_id}/finish",
        "/api/v1/executions/{execution_id}/steps",
        "/api/v1/executions/{execution_id}/steps/{step_id}",
        "/api/v1/executions/{execution_id}/attempts",
        "/api/v1/executions/{execution_id}/events",
        "/api/v1/executions/{execution_id}/artifacts",
        "/api/v1/executions/{execution_id}/trace",
        "/api/v1/artifacts/{artifact_id}",
    } <= set(paths)

    invalid_payload = _submit_payload()
    invalid_payload["idempotency_key"] = ""
    invalid_payload["metadata"] = {"password": "must-not-leak"}
    invalid = await client.post("/api/v1/executions", json=invalid_payload)
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "RequestValidationError"
    assert "must-not-leak" not in invalid.text


async def test_static_execution_rest_lifecycle_and_queries(
    rest_client: tuple[httpx.AsyncClient, ApplicationContainer],
) -> None:
    client, _ = rest_client
    capabilities = await client.get("/api/v1/capabilities")
    assert capabilities.status_code == 200
    assert capabilities.json()["execution_modes"] == ["STATIC", "DYNAMIC"]

    submitted = await client.post("/api/v1/executions", json=_submit_payload())
    assert submitted.status_code == 202
    body = submitted.json()
    execution_id = body["execution_id"]
    step_id = body["steps"][0]["step_id"]
    assert submitted.headers["location"] == f"/api/v1/executions/{execution_id}"
    assert body["status"] == "QUEUED"
    assert body["context"]["task_id"] == "rest-task"

    repeated = await client.post("/api/v1/executions", json=_submit_payload())
    assert repeated.status_code == 202
    assert repeated.json()["execution_id"] == execution_id

    fetched = await client.get(f"/api/v1/executions/{execution_id}")
    history = await client.get("/api/v1/executions", params={"task_id": "rest-task"})
    steps = await client.get(f"/api/v1/executions/{execution_id}/steps")
    step = await client.get(f"/api/v1/executions/{execution_id}/steps/{step_id}")
    attempts = await client.get(f"/api/v1/executions/{execution_id}/attempts")
    events = await client.get(f"/api/v1/executions/{execution_id}/events")
    artifacts = await client.get(f"/api/v1/executions/{execution_id}/artifacts")
    trace = await client.get(f"/api/v1/executions/{execution_id}/trace")

    assert fetched.status_code == 200
    assert [item["execution_id"] for item in history.json()["items"]] == [execution_id]
    assert history.json()["has_more"] is False
    assert steps.json()["items"][0]["step_id"] == step_id
    assert step.json()["plan_step_id"] == "plan-rest-1-step-0"
    assert attempts.json()["items"] == []
    assert events.json()["items"][0]["event_type"] == "execution.submitted"
    assert artifacts.json()["items"] == []
    assert trace.json()["execution"]["execution_id"] == execution_id
    assert trace.json()["events"]["items"][0]["delivery_status"] == "PENDING"
    assert body["created_by_type"] == "USER"
    assert body["created_by"] == "rest-user"

    cancelled = await client.post(
        f"/api/v1/executions/{execution_id}/cancel",
        json={
            "idempotency_key": "rest-cancel-1",
            "reason": "REST test",
            "actor": {"type": "USER", "id": "rest-user"},
        },
    )
    assert cancelled.status_code == 202
    assert cancelled.json()["status"] == "CANCEL_REQUESTED"


async def test_execution_history_cursor_pagination_and_invalid_cursor(
    rest_client: tuple[httpx.AsyncClient, ApplicationContainer],
) -> None:
    client, _ = rest_client
    submitted_ids: set[str] = set()
    for index in range(3):
        response = await client.post(
            "/api/v1/executions",
            json=_submit_payload(
                key=f"rest-page-{index}",
                plan_id=f"rest-page-plan-{index}",
            ),
        )
        assert response.status_code == 202
        submitted_ids.add(response.json()["execution_id"])

    first = await client.get("/api/v1/executions", params={"limit": 2})
    assert first.status_code == 200
    first_body = first.json()
    assert len(first_body["items"]) == 2
    assert first_body["has_more"] is True
    assert first_body["next_cursor"]

    second = await client.get(
        "/api/v1/executions",
        params={"limit": 2, "cursor": first_body["next_cursor"]},
    )
    assert second.status_code == 200
    second_body = second.json()
    returned_ids = {item["execution_id"] for item in first_body["items"] + second_body["items"]}
    assert returned_ids == submitted_ids
    assert second_body["has_more"] is False

    invalid = await client.get("/api/v1/executions", params={"cursor": "not-a-cursor"})
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "InvalidCursorError"


async def test_dynamic_continue_and_finish_rest_api(
    rest_client: tuple[httpx.AsyncClient, ApplicationContainer],
) -> None:
    client, container = rest_client
    submitted = await client.post(
        "/api/v1/executions",
        json=_submit_payload(key="rest-dynamic-1", mode="DYNAMIC", plan_id="dynamic-plan-1"),
    )
    execution_id = UUID(submitted.json()["execution_id"])

    async with container.session_factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution_id)
            .values(status=ExecutionStatus.WAITING_FOR_NEXT_STEP, version=1)
        )
        await session.execute(
            update(ExecutionStepORM)
            .where(ExecutionStepORM.execution_id == execution_id)
            .values(status=StepStatus.SUCCEEDED)
        )

    continued = await client.post(
        f"/api/v1/executions/{execution_id}/continue",
        json={
            "idempotency_key": "rest-continue-1",
            "expected_version": 1,
            "source": {
                "type": "INLINE",
                "spec": {
                    "schema_version": "1.0",
                    "execution_plan_id": "dynamic-plan-2",
                    "steps": [
                        {
                            "sequence": 1,
                            "plan_step_id": "dynamic-plan-2-step-1",
                            "code": "print('next dynamic step')",
                        }
                    ],
                },
            },
            "actor": {"type": "USER", "id": "rest-user"},
        },
    )
    assert continued.status_code == 202
    assert continued.json()["status"] == "QUEUED"
    assert [step["sequence"] for step in continued.json()["steps"]] == [0, 1]

    async with container.session_factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution_id)
            .values(status=ExecutionStatus.WAITING_FOR_NEXT_STEP, version=3)
        )

    finished = await client.post(
        f"/api/v1/executions/{execution_id}/finish",
        json={
            "idempotency_key": "rest-finish-1",
            "expected_version": 3,
            "actor": {"type": "USER", "id": "rest-user"},
        },
    )
    assert finished.status_code == 202
    assert finished.json()["status"] == "QUEUED"
    assert finished.json()["version"] == 4


async def test_path_execution_spec_rest_submit(
    rest_client: tuple[httpx.AsyncClient, ApplicationContainer],
) -> None:
    client, container = rest_client
    spec = {
        "schema_version": "1.0",
        "execution_plan_id": "path-plan",
        "steps": [
            {
                "sequence": 0,
                "plan_step_id": "path-plan-step-0",
                "code": "print('PATH source')",
            }
        ],
    }
    content = json.dumps(spec, separators=(",", ":")).encode()
    relative_path = Path("plans/path-plan/execution-spec.json")
    source_path = container.settings.workspace_host_root / relative_path
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(content)
    payload = _submit_payload(key="rest-path-submit", plan_id="ignored-inline-plan")
    payload["source"] = {
        "type": "PATH",
        "path": relative_path.as_posix(),
        "sha256": hashlib.sha256(content).hexdigest(),
    }

    submitted = await client.post("/api/v1/executions", json=payload)

    assert submitted.status_code == 202
    assert submitted.json()["source"] == {
        "type": "PATH",
        "path": relative_path.as_posix(),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    assert submitted.json()["context"]["execution_plan_id"] == "path-plan"


async def test_retry_and_domain_error_mapping(
    rest_client: tuple[httpx.AsyncClient, ApplicationContainer],
) -> None:
    client, container = rest_client
    payload = _submit_payload(key="rest-retry-submit", plan_id="retry-plan")
    submitted = await client.post("/api/v1/executions", json=payload)
    execution_id = UUID(submitted.json()["execution_id"])

    conflicting = deepcopy(payload)
    conflicting["metadata"] = {"different": True}
    conflict = await client.post("/api/v1/executions", json=conflicting)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IdempotencyConflictError"

    async with container.session_factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution_id)
            .values(
                status=ExecutionStatus.FAILED,
                retryable=True,
                retry_strategy=RetryStrategy.FROM_START,
                retry_from_sequence=0,
            )
        )

    retried = await client.post(
        f"/api/v1/executions/{execution_id}/retry",
        json={
            "idempotency_key": "rest-retry-1",
            "actor": {"type": "USER", "id": "rest-user"},
        },
    )
    assert retried.status_code == 202
    assert retried.json()["status"] == "QUEUED"
    assert retried.json()["retry_count"] == 1

    missing_execution = await client.get(f"/api/v1/executions/{uuid4()}")
    missing_artifact = await client.get(f"/api/v1/artifacts/{uuid4()}")
    assert missing_execution.status_code == 404
    assert missing_execution.json()["error"]["code"] == "ExecutionNotFoundError"
    assert missing_artifact.status_code == 404
    assert missing_artifact.json()["error"]["code"] == "ExecutionArtifactNotFoundError"
