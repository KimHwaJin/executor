import hashlib
import json
from collections.abc import AsyncIterator
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest_asyncio
from sqlalchemy import update

from executor_service.config import Settings
from executor_service.container import ApplicationContainer
from executor_service.domain.enums import (
    AttemptStatus,
    ExecutionStatus,
    OperationStatus,
    RetryStrategy,
    RuntimePool,
    RuntimeTargetStatus,
    RuntimeType,
    StepStatus,
)
from executor_service.domain.models import utc_now
from executor_service.domain.runtime import RuntimeStorageAccess
from executor_service.infrastructure.db.base import Base
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionOperationORM,
    ExecutionORM,
    ExecutionStepAttemptORM,
    ExecutionStepORM,
    RuntimeTargetORM,
)
from executor_service.interfaces.http.app import create_app


@pytest_asyncio.fixture
async def rest_client(
    tmp_path: Path,
) -> AsyncIterator[tuple[httpx.AsyncClient, ApplicationContainer]]:
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        redis_url="redis://localhost:6399/15",
        runtime_enabled=False,
        input_host_root=tmp_path,
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
        "runtime_profile": "basic",
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
            "user_id": "rest-user",
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
        "/api/v1/executions",
        "/api/v1/executions/{execution_id}",
        "/api/v1/executions/{execution_id}/cancel",
        "/api/v1/executions/{execution_id}/retry",
        "/api/v1/executions/{execution_id}/continue",
        "/api/v1/executions/{execution_id}/finish",
        "/api/v1/executions/{execution_id}/notebook",
        "/api/v1/executions/{execution_id}/notebook/cells/{cell_index}",
        "/api/v1/executions/{execution_id}/steps",
        "/api/v1/executions/{execution_id}/steps/{step_id}",
        "/api/v1/executions/{execution_id}/attempts",
        "/api/v1/executions/{execution_id}/attempts/{attempt_id}",
        "/api/v1/executions/{execution_id}/attempts/{attempt_id}/steps",
        "/api/v1/executions/{execution_id}/events",
        "/api/v1/executions/{execution_id}/artifacts",
        "/api/v1/artifacts/{artifact_id}",
    } <= set(paths)

    invalid_payload = _submit_payload()
    invalid_payload["idempotency_key"] = ""
    invalid_payload["metadata"] = {"password": "must-not-leak"}
    invalid = await client.post("/api/v1/executions", json=invalid_payload)
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "REQUEST_VALIDATION_ERROR"
    assert "must-not-leak" not in invalid.text


async def test_rest_reads_runtime_owned_notebook_and_cell_outputs(
    rest_client: tuple[httpx.AsyncClient, ApplicationContainer],
) -> None:
    client, container = rest_client
    submitted = await client.post("/api/v1/executions", json=_submit_payload())
    execution_id = submitted.json()["execution_id"]

    unavailable = await client.get(f"/api/v1/executions/{execution_id}/notebook")
    assert unavailable.status_code == 409
    assert unavailable.json()["error"]["code"] == "EXECUTION_NOTEBOOK_NOT_AVAILABLE"

    relative = f"users/rest-user/{execution_id}/notebooks/execution.ipynb"
    target_id = uuid4()
    notebook = {
        "metadata": {},
        "cells": [
            {
                "id": "cell-1",
                "cell_type": "code",
                "source": "print('first')\nprint('second')",
                "execution_count": 1,
                "metadata": {},
                "outputs": [
                    {"output_type": "stream", "name": "stdout", "text": "first\nsecond\n"}
                ],
            }
        ],
    }
    container.notebook_queries._runtime_storage = _NotebookStorage(notebook)
    async with container.session_factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == UUID(execution_id))
            .values(runtime_target_id=target_id, notebook_path=relative)
        )

    brief = await client.get(
        f"/api/v1/executions/{execution_id}/notebook", params={"response_format": "brief"}
    )
    assert brief.status_code == 200
    assert brief.json()["cells"][0]["source"] == "print('first')"
    assert brief.json()["cells"][0]["outputs"] == []

    cell = await client.get(f"/api/v1/executions/{execution_id}/notebook/cells/0")
    assert cell.status_code == 200
    assert cell.json()["cell"]["outputs"][0]["text"] == "first\nsecond\n"

    missing = await client.get(f"/api/v1/executions/{execution_id}/notebook/cells/3")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOTEBOOK_CELL_NOT_FOUND"


class _NotebookStorage(RuntimeStorageAccess):
    def __init__(self, notebook: dict[str, Any]) -> None:
        self.notebook = notebook

    async def read_notebook(
        self,
        runtime_type: RuntimeType,
        preferred_target_id: UUID | None,
        path: str,
    ) -> dict[str, Any]:
        del runtime_type, preferred_target_id, path
        return self.notebook


async def test_static_execution_rest_lifecycle_and_queries(
    rest_client: tuple[httpx.AsyncClient, ApplicationContainer],
) -> None:
    client, _ = rest_client
    submitted = await client.post("/api/v1/executions", json=_submit_payload())
    assert submitted.status_code == 202
    body = submitted.json()
    execution_id = body["execution_id"]
    assert submitted.headers["location"] == f"/api/v1/executions/{execution_id}"
    assert body["state"]["status"] == "QUEUED"
    assert set(body) == {
        "execution_id",
        "operation_id",
        "state",
        "created_by_type",
        "created_by",
        "updated_by_type",
        "updated_by",
        "created_at",
        "updated_at",
    }

    repeated = await client.post("/api/v1/executions", json=_submit_payload())
    assert repeated.status_code == 202
    assert repeated.json()["execution_id"] == execution_id

    fetched = await client.get(f"/api/v1/executions/{execution_id}")
    history = await client.get("/api/v1/executions", params={"user_id": "rest-user"})
    steps = await client.get(f"/api/v1/executions/{execution_id}/steps")
    step_id = steps.json()["items"][0]["step_id"]
    step = await client.get(f"/api/v1/executions/{execution_id}/steps/{step_id}")
    attempts = await client.get(f"/api/v1/executions/{execution_id}/attempts")
    events = await client.get(f"/api/v1/executions/{execution_id}/events")
    artifacts = await client.get(f"/api/v1/executions/{execution_id}/artifacts")

    assert fetched.status_code == 200
    assert fetched.json()["runtime"]["type"] == "JUPYTER"
    assert [item["execution_id"] for item in history.json()["items"]] == [execution_id]
    assert "runtime" not in history.json()["items"][0]
    assert "steps" not in fetched.json()
    assert history.json()["has_more"] is False
    assert steps.json()["items"][0]["step_id"] == step_id
    assert steps.json()["items"][0]["execution_id"] == execution_id
    assert step.json()["plan"]["plan_step_id"] == "plan-rest-1-step-0"
    assert attempts.json()["items"] == []
    assert events.json()["items"][0]["event_type"] == "execution.submitted"
    assert events.json()["items"][0]["payload"]["schema_version"] == "1.0"
    assert artifacts.json()["items"] == []
    assert events.json()["items"][0]["delivery"]["status"] == "PENDING"
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
    assert cancelled.json()["state"]["status"] == "CANCEL_REQUESTED"
    assert cancelled.headers["location"] == f"/api/v1/executions/{execution_id}"


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
    assert invalid.json()["error"]["code"] == "INVALID_CURSOR"


async def test_attempt_detail_and_step_attempt_routes(
    rest_client: tuple[httpx.AsyncClient, ApplicationContainer],
) -> None:
    client, container = rest_client
    submitted = await client.post(
        "/api/v1/executions",
        json=_submit_payload(key="rest-attempt-submit", plan_id="rest-attempt-plan"),
    )
    execution_id = UUID(submitted.json()["execution_id"])
    steps = await client.get(f"/api/v1/executions/{execution_id}/steps")
    step_id = UUID(steps.json()["items"][0]["step_id"])
    target_id = uuid4()
    attempt_id = uuid4()
    now = utc_now()
    async with container.session_factory() as session, session.begin():
        session.add(
            RuntimeTargetORM(
                id=target_id,
                name="rest-attempt-jupyter",
                connection_config={"endpoint": "http://127.0.0.1:8888"},
                credential_ref="settings:JUPYTER_TOKEN",
                pool=RuntimePool.INTERACTIVE,
                status=RuntimeTargetStatus.ACTIVE,
                max_concurrent_executions=1,
                supported_profiles=["basic"],
                enabled=True,
            )
        )
        session.add(
            ExecutionAttemptORM(
                id=attempt_id,
                execution_id=execution_id,
                attempt_number=1,
                runtime_target_id=target_id,
                runtime_session_id="rest-kernel",
                status=AttemptStatus.RUNNING,
                lease_owner="rest-worker",
                lease_expires_at=now + timedelta(minutes=1),
                heartbeat_at=now,
                started_at=now,
            )
        )
        session.add(
            ExecutionStepAttemptORM(
                execution_id=execution_id,
                execution_attempt_id=attempt_id,
                execution_step_id=step_id,
                sequence=0,
                skill_name="data_load",
                tool_name="load_data",
                input_parameters={},
                status=StepStatus.RUNNING,
                outputs=[],
                started_at=now,
            )
        )

    attempts = await client.get(f"/api/v1/executions/{execution_id}/attempts")
    detail = await client.get(f"/api/v1/executions/{execution_id}/attempts/{attempt_id}")
    attempt_steps = await client.get(
        f"/api/v1/executions/{execution_id}/attempts/{attempt_id}/steps"
    )

    assert attempts.json()["items"][0]["step_count"] == 1
    assert "runtime" not in attempts.json()["items"][0]
    assert detail.json()["runtime"]["session_id"] == "rest-kernel"
    assert detail.json()["lease"]["owner"] == "rest-worker"
    assert attempt_steps.json()["items"][0]["execution_step_id"] == str(step_id)
    assert (
        await client.get(f"/api/v1/executions/{uuid4()}/attempts/{attempt_id}")
    ).status_code == 404
    wrong_parent = await client.get(f"/api/v1/executions/{execution_id}/attempts/{uuid4()}/steps")
    assert wrong_parent.status_code == 404
    assert wrong_parent.json()["error"]["code"] == "EXECUTION_ATTEMPT_NOT_FOUND"


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
            .values(status=ExecutionStatus.WAITING_FOR_CONTINUE, version=1)
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
                        },
                        {
                            "sequence": 2,
                            "plan_step_id": "dynamic-plan-2-step-2",
                            "code": "print('another dynamic step')",
                        },
                    ],
                },
            },
            "actor": {"type": "USER", "id": "rest-user"},
        },
    )
    assert continued.status_code == 202
    assert continued.json()["state"]["status"] == "QUEUED"
    continued_repeat = await client.post(
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
                        },
                        {
                            "sequence": 2,
                            "plan_step_id": "dynamic-plan-2-step-2",
                            "code": "print('another dynamic step')",
                        },
                    ],
                },
            },
            "actor": {"type": "USER", "id": "rest-user"},
        },
    )
    assert continued_repeat.status_code == 202
    assert continued_repeat.json()["operation_id"] == continued.json()["operation_id"]
    continued_steps = await client.get(f"/api/v1/executions/{execution_id}/steps")
    assert [step["sequence"] for step in continued_steps.json()["items"]] == [0, 1, 2]
    operation_id = continued.json()["operation_id"]
    operation = await client.get(f"/api/v1/executions/{execution_id}/operations/{operation_id}")
    assert operation.status_code == 200
    assert operation.json()["sequence_range"] == {"first": 1, "last": 2}
    operation_steps = await client.get(
        f"/api/v1/executions/{execution_id}/operations/{operation_id}/steps"
    )
    assert [step["sequence"] for step in operation_steps.json()["items"]] == [1, 2]

    async with container.session_factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution_id)
            .values(status=ExecutionStatus.WAITING_FOR_CONTINUE, version=3)
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
    assert finished.json()["state"]["status"] == "QUEUED"
    assert finished.json()["state"]["version"] == 4


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
    source_path = container.settings.input_host_root / relative_path
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
    fetched = await client.get(submitted.headers["location"])
    assert fetched.json()["source"] == {
        "type": "PATH",
        "path": relative_path.as_posix(),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    assert fetched.json()["context"]["execution_plan_id"] == "path-plan"


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
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    async with container.session_factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution_id)
            .values(
                status=ExecutionStatus.FAILED,
                retry_strategy=RetryStrategy.FROM_START,
                retry_from_sequence=0,
            )
        )
        await session.execute(
            update(ExecutionOperationORM)
            .where(ExecutionOperationORM.execution_id == execution_id)
            .values(status=OperationStatus.FAILED)
        )

    retried = await client.post(
        f"/api/v1/executions/{execution_id}/retry",
        json={
            "idempotency_key": "rest-retry-1",
            "actor": {"type": "USER", "id": "rest-user"},
        },
    )
    assert retried.status_code == 202
    assert retried.json()["state"]["status"] == "QUEUED"
    assert retried.json()["operation_id"] == submitted.json()["operation_id"]
    fetched = await client.get(f"/api/v1/executions/{execution_id}")
    assert fetched.json()["retry"]["count"] == 1

    missing_execution = await client.get(f"/api/v1/executions/{uuid4()}")
    missing_artifact = await client.get(f"/api/v1/artifacts/{uuid4()}")
    assert missing_execution.status_code == 404
    assert missing_execution.json()["error"]["code"] == "EXECUTION_NOT_FOUND"
    assert missing_artifact.status_code == 404
    assert missing_artifact.json()["error"]["code"] == "ARTIFACT_NOT_FOUND"
