import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest_asyncio
from sqlalchemy import update

from executor_service.application.artifact_content import (
    ArtifactContent,
)
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
from executor_service.domain.errors import ArtifactRangeNotSatisfiableError
from executor_service.domain.models import utc_now
from executor_service.domain.runtime import (
    RuntimeByteRange,
    RuntimeFileMetadata,
    RuntimeStorageAccess,
)
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
from tests.runtime_credentials import runtime_credential_fields


@pytest_asyncio.fixture
async def rest_client(
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
    app = create_app(container)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        yield client, container
    await container.redis.aclose()
    await container.engine.dispose()


def _submit_payload(
    *,
    key: str = "rest-submit-1",
    operation_mode: str = "SINGLE",
) -> dict[str, Any]:
    return {
        "idempotency_key": key,
        "lifecycle": {
            "operation_mode": operation_mode,
            **(
                {"operation_wait_timeout_seconds": 600}
                if operation_mode == "MULTI"
                else {}
            ),
        },
        "trigger": {
            "type": "INTERACTIVE",
            "actor": {"type": "USER", "id": "rest-user"},
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
                                "content": "print('hello from REST')",
                            },
                        },
                        "lineage": {
                            "skill_name": "data_load",
                            "tool_name": "load_data",
                            "input_parameters": {"product": "A"},
                        },
                    }
                ],
            },
            "metadata": {"phase": "initial"},
        },
        "context": {
            "user_id": "rest-user",
            "project_id": "rest-project",
            "session_id": "rest-session",
            "task_id": "rest-task",
        },
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
    expected_methods = {
        "/api/v1/executions": {"get", "post"},
        "/api/v1/executions/{execution_id}": {"get"},
        "/api/v1/executions/{execution_id}/result": {"get"},
        "/api/v1/executions/{execution_id}/cancel": {"post"},
        "/api/v1/executions/{execution_id}/retry": {"post"},
        "/api/v1/executions/{execution_id}/operations": {"get", "post"},
        ("/api/v1/executions/{execution_id}/operations/{operation_id}"): {
            "get"
        },
        (
            "/api/v1/executions/{execution_id}/operations/"
            "{operation_id}/result"
        ): {"get"},
        (
            "/api/v1/executions/{execution_id}/operations/{operation_id}/steps"
        ): {"get"},
        "/api/v1/executions/{execution_id}/finalize": {"post"},
        "/api/v1/executions/{execution_id}/notebook": {"get"},
        ("/api/v1/executions/{execution_id}/notebook/cells/{cell_index}"): {
            "get"
        },
        "/api/v1/executions/{execution_id}/steps": {"get"},
        "/api/v1/executions/{execution_id}/steps/{step_id}": {"get"},
        "/api/v1/executions/{execution_id}/artifacts": {"get", "post"},
        "/api/v1/executions/{execution_id}/attempts": {"get"},
        ("/api/v1/executions/{execution_id}/attempts/{attempt_id}"): {"get"},
        ("/api/v1/executions/{execution_id}/attempts/{attempt_id}/steps"): {
            "get"
        },
        "/api/v1/executions/{execution_id}/events": {"get"},
        "/api/v1/artifacts/{artifact_id}": {"get"},
        "/api/v1/artifacts/{artifact_id}/content": {"get"},
    }
    assert expected_methods.keys() <= paths.keys()
    assert {
        path: set(paths[path]) for path in expected_methods
    } == expected_methods

    invalid_payload = _submit_payload()
    invalid_payload["idempotency_key"] = ""
    invalid_payload["metadata"] = {"password": "must-not-leak"}
    invalid = await client.post("/api/v1/executions", json=invalid_payload)
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "REQUEST_VALIDATION_ERROR"
    assert "must-not-leak" not in invalid.text


async def test_artifact_content_endpoint_streams_ranges_with_metadata(
    rest_client: tuple[httpx.AsyncClient, ApplicationContainer],
) -> None:
    client, container = rest_client
    artifact_id = uuid4()

    class _ContentService:
        @asynccontextmanager
        async def open(
            self, requested_id: UUID, range_header: str | None
        ) -> AsyncIterator[ArtifactContent]:
            assert requested_id == artifact_id
            assert range_header == "bytes=2-5"

            async def body() -> AsyncIterator[bytes]:
                yield b"2345"

            yield ArtifactContent(
                name="plot image.png",
                media_type="image/png",
                checksum_sha256="b" * 64,
                byte_range=RuntimeByteRange(
                    start=2,
                    end=5,
                    size=10,
                    partial=True,
                ),
                body=body(),
            )

    cast(Any, container).artifact_content = _ContentService()
    response = await client.get(
        f"/api/v1/artifacts/{artifact_id}/content",
        headers={"Range": "bytes=2-5"},
    )

    assert response.status_code == 206
    assert response.content == b"2345"
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-range"] == "bytes 2-5/10"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-length"] == "4"
    assert response.headers["etag"] == f'"{"b" * 64}"'
    assert "plot%20image.png" in response.headers["content-disposition"]


async def test_artifact_content_endpoint_returns_range_contract(
    rest_client: tuple[httpx.AsyncClient, ApplicationContainer],
) -> None:
    client, container = rest_client

    class _RejectingContentService:
        @asynccontextmanager
        async def open(
            self, _artifact_id: UUID, _range_header: str | None
        ) -> AsyncIterator[ArtifactContent]:
            raise ArtifactRangeNotSatisfiableError("invalid range", 10)
            yield  # pragma: no cover

    cast(Any, container).artifact_content = _RejectingContentService()
    response = await client.get(
        f"/api/v1/artifacts/{uuid4()}/content",
        headers={"Range": "bytes=20-30"},
    )

    assert response.status_code == 416
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-range"] == "bytes */10"
    assert response.json()["error"]["code"] == (
        "ARTIFACT_RANGE_NOT_SATISFIABLE"
    )


async def test_submit_contract_validates_lifecycle_and_optional_scope(
    rest_client: tuple[httpx.AsyncClient, ApplicationContainer],
) -> None:
    client, _ = rest_client

    single_with_wait = _submit_payload(key="single-with-wait")
    single_with_wait["lifecycle"]["operation_wait_timeout_seconds"] = 600
    response = await client.post("/api/v1/executions", json=single_with_wait)
    assert response.status_code == 422

    multi_without_wait = _submit_payload(
        key="multi-without-wait", operation_mode="MULTI"
    )
    multi_without_wait["lifecycle"].pop("operation_wait_timeout_seconds")
    response = await client.post("/api/v1/executions", json=multi_without_wait)
    assert response.status_code == 422

    unscoped = _submit_payload(key="optional-scope")
    unscoped["context"].pop("project_id")
    unscoped["context"].pop("session_id")
    response = await client.post("/api/v1/executions", json=unscoped)
    assert response.status_code == 202
    fetched = await client.get(response.headers["location"])
    assert fetched.json()["context"]["project_id"] is None
    assert fetched.json()["context"]["session_id"] is None

    session_without_project = _submit_payload(key="session-without-project")
    session_without_project["context"].pop("project_id")
    response = await client.post(
        "/api/v1/executions", json=session_without_project
    )
    assert response.status_code == 422

    reserved_scope = _submit_payload(key="reserved-scope")
    reserved_scope["context"]["project_id"] = "unscoped"
    response = await client.post("/api/v1/executions", json=reserved_scope)
    assert response.status_code == 422


async def test_rest_reads_runtime_owned_notebook_and_cell_outputs(
    rest_client: tuple[httpx.AsyncClient, ApplicationContainer],
) -> None:
    client, container = rest_client
    submitted = await client.post("/api/v1/executions", json=_submit_payload())
    execution_id = submitted.json()["execution_id"]

    unavailable = await client.get(
        f"/api/v1/executions/{execution_id}/notebook"
    )
    assert unavailable.status_code == 409
    assert (
        unavailable.json()["error"]["code"]
        == "EXECUTION_NOTEBOOK_NOT_AVAILABLE"
    )

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
                    {
                        "output_type": "stream",
                        "name": "stdout",
                        "text": "first\nsecond\n",
                    }
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

    summary = await client.get(
        f"/api/v1/executions/{execution_id}/notebook",
        params={"view": "SUMMARY"},
    )
    assert summary.status_code == 200
    assert summary.json()["cells"][0]["source_preview"].startswith(
        "print('first')"
    )
    assert "outputs" not in summary.json()["cells"][0]
    assert summary.json()["cells"][0]["output_summary"] == {
        "output_count": 1,
        "output_types": {"stream": 1},
        "stream_names": ["stdout"],
        "mime_types": [],
        "has_image": False,
        "image_count": 0,
        "has_error": False,
    }

    cell = await client.get(
        f"/api/v1/executions/{execution_id}/notebook/cells/0"
    )
    assert cell.status_code == 200
    assert cell.json()["cell"]["outputs"][0]["text"] == "first\nsecond\n"

    missing = await client.get(
        f"/api/v1/executions/{execution_id}/notebook/cells/3"
    )
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

    async def write_notebook(
        self,
        runtime_type: RuntimeType,
        preferred_target_id: UUID | None,
        path: str,
        notebook: dict[str, Any],
    ) -> None:
        self.notebook = notebook

    async def write_text(
        self,
        runtime_type: RuntimeType,
        preferred_target_id: UUID | None,
        path: str,
        content: str,
    ) -> RuntimeFileMetadata:
        raw = content.encode()
        return RuntimeFileMetadata(
            path=path,
            name=Path(path).name,
            size_bytes=len(raw),
            modified_ns=1,
            media_type="text/markdown",
            checksum_sha256=hashlib.sha256(raw).hexdigest(),
        )


async def test_consolidated_result_returns_operation_steps_in_one_call(
    rest_client: tuple[httpx.AsyncClient, ApplicationContainer],
) -> None:
    client, container = rest_client
    submitted = await client.post(
        "/api/v1/executions", json=_submit_payload(key="rest-result-submit")
    )
    execution_id = UUID(submitted.json()["execution_id"])
    operation_id = UUID(submitted.json()["operation"]["operation_id"])
    async with container.session_factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution_id)
            .values(status=ExecutionStatus.SUCCEEDED)
        )
        await session.execute(
            update(ExecutionOperationORM)
            .where(ExecutionOperationORM.id == operation_id)
            .values(status=OperationStatus.SUCCEEDED)
        )
        await session.execute(
            update(ExecutionStepORM)
            .where(ExecutionStepORM.execution_id == execution_id)
            .values(
                status=StepStatus.SUCCEEDED,
                output_summary={
                    "output_count": 1,
                    "output_types": {"stream": 1},
                    "stream_names": ["stdout"],
                    "mime_types": [],
                    "has_image": False,
                    "image_count": 0,
                    "has_error": False,
                },
            )
        )

    result = await client.get(f"/api/v1/executions/{execution_id}/result")
    operation_result = await client.get(
        f"/api/v1/executions/{execution_id}/operations/{operation_id}/result"
    )

    assert result.status_code == 200
    assert set(result.json()) == {
        "execution",
        "operations",
        "attempts",
        "artifacts",
    }
    assert set(result.json()["execution"]) == {"execution_id", "state"}
    assert set(result.json()["operations"][0]) == {
        "operation_id",
        "operation_number",
        "sequence_range",
        "result",
        "lifecycle",
        "steps",
    }
    assert set(result.json()["operations"][0]["steps"][0]) == {
        "step_id",
        "sequence",
        "lineage",
        "result",
        "lifecycle",
    }
    assert result.json()["execution"]["state"]["status"] == "SUCCEEDED"
    step_result = result.json()["operations"][0]["steps"][0]["result"]
    assert "outputs" not in step_result
    assert step_result["output_summary"]["output_count"] == 1
    assert step_result["output_summary"]["stream_names"] == ["stdout"]
    assert operation_result.status_code == 200
    assert operation_result.json()["operation"]["operation_id"] == str(
        operation_id
    )


async def test_materializes_final_report_below_runtime_reports_directory(
    rest_client: tuple[httpx.AsyncClient, ApplicationContainer],
) -> None:
    client, container = rest_client
    submitted = await client.post(
        "/api/v1/executions", json=_submit_payload(key="rest-report-submit")
    )
    execution_id = UUID(submitted.json()["execution_id"])
    workspace = f"users/rest-user/projects/rest-project/sessions/rest-session/executions/{execution_id}"
    storage = _NotebookStorage(
        {"nbformat": 4, "nbformat_minor": 5, "metadata": {}, "cells": []}
    )
    container.materialized_artifacts._runtime_storage = storage
    async with container.session_factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution_id)
            .values(
                status=ExecutionStatus.SUCCEEDED,
                workspace_path=workspace,
                notebook_path=f"{workspace}/notebooks/execution.ipynb",
            )
        )

    response = await client.post(
        f"/api/v1/executions/{execution_id}/artifacts",
        json={
            "idempotency_key": "report-materialize-1",
            "type": "REPORT",
            "source": {"type": "INLINE", "content": "# Final report\n\nDone."},
            "append_to_notebook": True,
            "actor": {"type": "USER", "id": "rest-user"},
        },
    )

    assert response.status_code == 201
    assert (
        response.json()["storage"]["relative_path"]
        == f"{workspace}/reports/final-report.md"
    )
    assert response.json()["storage"]["media_type"] == "text/markdown"
    assert storage.notebook["cells"][-1]["cell_type"] == "markdown"


async def test_single_execution_rest_lifecycle_and_queries(
    rest_client: tuple[httpx.AsyncClient, ApplicationContainer],
) -> None:
    client, _ = rest_client
    submitted = await client.post("/api/v1/executions", json=_submit_payload())
    assert submitted.status_code == 202
    body = submitted.json()
    execution_id = body["execution_id"]
    assert (
        submitted.headers["location"] == f"/api/v1/executions/{execution_id}"
    )
    assert body["state"]["status"] == "QUEUED"
    assert body["operation"]["steps"][0]["sequence"] == 0
    assert body["operation"]["steps"][0]["step_id"]
    assert set(body) == {
        "execution_id",
        "operation",
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
    history = await client.get(
        "/api/v1/executions", params={"user_id": "rest-user"}
    )
    steps = await client.get(f"/api/v1/executions/{execution_id}/steps")
    step_id = steps.json()["items"][0]["step_id"]
    step = await client.get(
        f"/api/v1/executions/{execution_id}/steps/{step_id}"
    )
    attempts = await client.get(f"/api/v1/executions/{execution_id}/attempts")
    events = await client.get(f"/api/v1/executions/{execution_id}/events")
    artifacts = await client.get(
        f"/api/v1/executions/{execution_id}/artifacts"
    )

    assert fetched.status_code == 200
    assert fetched.json()["runtime"]["type"] == "JUPYTER"
    assert fetched.json()["workspace"]["notebook_projection"] == {
        "status": "NOT_STARTED",
        "attempt_count": 0,
        "error_message": None,
        "projected_at": None,
    }
    assert [item["execution_id"] for item in history.json()["items"]] == [
        execution_id
    ]
    assert "runtime" not in history.json()["items"][0]
    assert "steps" not in fetched.json()
    assert history.json()["has_more"] is False
    assert steps.json()["items"][0]["step_id"] == step_id
    assert steps.json()["items"][0]["execution_id"] == execution_id
    assert step.json()["lineage"]["tool_name"] == "load_data"
    assert attempts.json()["items"] == []
    assert events.json()["items"] == []
    assert artifacts.json()["items"] == []
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
    assert (
        cancelled.headers["location"] == f"/api/v1/executions/{execution_id}"
    )


async def test_execution_history_cursor_pagination_and_invalid_cursor(
    rest_client: tuple[httpx.AsyncClient, ApplicationContainer],
) -> None:
    client, _ = rest_client
    submitted_ids: set[str] = set()
    for index in range(3):
        response = await client.post(
            "/api/v1/executions",
            json=_submit_payload(key=f"rest-page-{index}"),
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
    returned_ids = {
        item["execution_id"]
        for item in first_body["items"] + second_body["items"]
    }
    assert returned_ids == submitted_ids
    assert second_body["has_more"] is False

    invalid = await client.get(
        "/api/v1/executions", params={"cursor": "not-a-cursor"}
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "INVALID_CURSOR"


async def test_attempt_detail_and_step_attempt_routes(
    rest_client: tuple[httpx.AsyncClient, ApplicationContainer],
) -> None:
    client, container = rest_client
    submitted = await client.post(
        "/api/v1/executions",
        json=_submit_payload(key="rest-attempt-submit"),
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
                **runtime_credential_fields(),
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
                status=StepStatus.SUCCEEDED,
                result_fencing_token=1,
                result_manifest_path=(
                    "executions/e/operations/o/steps/s/attempts/a/1/"
                    "manifest.json"
                ),
                result_manifest_checksum_sha256="c" * 64,
                result_manifest_size_bytes=128,
                result_complete=True,
                result_representation_count=1,
                result_total_size_bytes=4,
                started_at=now,
                finished_at=now,
            )
        )

    attempts = await client.get(f"/api/v1/executions/{execution_id}/attempts")
    detail = await client.get(
        f"/api/v1/executions/{execution_id}/attempts/{attempt_id}"
    )
    attempt_steps = await client.get(
        f"/api/v1/executions/{execution_id}/attempts/{attempt_id}/steps"
    )

    assert attempts.json()["items"][0]["step_count"] == 1
    assert "runtime" not in attempts.json()["items"][0]
    assert detail.json()["runtime"]["session_id"] == "rest-kernel"
    assert detail.json()["lease"]["owner"] == "rest-worker"
    assert attempt_steps.json()["items"][0]["execution_step_id"] == str(
        step_id
    )
    result_ref = attempt_steps.json()["items"][0]["result"]["result_ref"]
    assert result_ref["attempt_id"] == str(attempt_id)
    assert result_ref["relative_path"].endswith("manifest.json")
    assert result_ref["complete"] is True
    assert (
        await client.get(f"/api/v1/executions/{uuid4()}/attempts/{attempt_id}")
    ).status_code == 404
    wrong_parent = await client.get(
        f"/api/v1/executions/{execution_id}/attempts/{uuid4()}/steps"
    )
    assert wrong_parent.status_code == 404
    assert (
        wrong_parent.json()["error"]["code"] == "EXECUTION_ATTEMPT_NOT_FOUND"
    )


async def test_multi_operation_create_and_finalize_rest_api(
    rest_client: tuple[httpx.AsyncClient, ApplicationContainer],
) -> None:
    client, container = rest_client
    submitted = await client.post(
        "/api/v1/executions",
        json=_submit_payload(key="rest-multi-1", operation_mode="MULTI"),
    )
    execution_id = UUID(submitted.json()["execution_id"])

    async with container.session_factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution_id)
            .values(status=ExecutionStatus.WAITING_FOR_OPERATION, version=1)
        )
        await session.execute(
            update(ExecutionStepORM)
            .where(ExecutionStepORM.execution_id == execution_id)
            .values(status=StepStatus.SUCCEEDED)
        )

    continued = await client.post(
        f"/api/v1/executions/{execution_id}/operations",
        json={
            "idempotency_key": "rest-operation-1",
            "expected_version": 1,
            "operation_timeout_seconds": 900,
            "metadata": {"phase": "follow-up"},
            "spec": {
                "schema_version": "1.0",
                "steps": [
                    {
                        "sequence": 1,
                        "payload": {
                            "type": "PYTHON_EXECUTE",
                            "source": {
                                "type": "INLINE",
                                "content": "print('next MULTI step')",
                            },
                        },
                    },
                    {
                        "sequence": 2,
                        "payload": {
                            "type": "PYTHON_EXECUTE",
                            "source": {
                                "type": "INLINE",
                                "content": "print('another MULTI step')",
                            },
                        },
                    },
                ],
            },
            "actor": {"type": "USER", "id": "rest-user"},
        },
    )
    assert continued.status_code == 202
    assert continued.json()["state"]["status"] == "QUEUED"
    operation_id = continued.json()["operation"]["operation_id"]
    assert continued.headers["location"] == (
        f"/api/v1/executions/{execution_id}/operations/{operation_id}"
    )
    continued_repeat = await client.post(
        f"/api/v1/executions/{execution_id}/operations",
        json={
            "idempotency_key": "rest-operation-1",
            "expected_version": 1,
            "operation_timeout_seconds": 900,
            "metadata": {"phase": "follow-up"},
            "spec": {
                "schema_version": "1.0",
                "steps": [
                    {
                        "sequence": 1,
                        "payload": {
                            "type": "PYTHON_EXECUTE",
                            "source": {
                                "type": "INLINE",
                                "content": "print('next MULTI step')",
                            },
                        },
                    },
                    {
                        "sequence": 2,
                        "payload": {
                            "type": "PYTHON_EXECUTE",
                            "source": {
                                "type": "INLINE",
                                "content": "print('another MULTI step')",
                            },
                        },
                    },
                ],
            },
            "actor": {"type": "USER", "id": "rest-user"},
        },
    )
    assert continued_repeat.status_code == 202
    assert (
        continued_repeat.json()["operation"] == continued.json()["operation"]
    )
    continued_steps = await client.get(
        f"/api/v1/executions/{execution_id}/steps"
    )
    assert [step["sequence"] for step in continued_steps.json()["items"]] == [
        0,
        1,
        2,
    ]
    operations = await client.get(
        f"/api/v1/executions/{execution_id}/operations"
    )
    operation_summary = operations.json()["items"][-1]
    assert operation_summary["operation_id"] == operation_id
    assert "schema_version" not in operation_summary
    assert "metadata" not in operation_summary
    assert "execution_attempt_id" not in operation_summary
    operation = await client.get(
        f"/api/v1/executions/{execution_id}/operations/{operation_id}"
    )
    assert operation.status_code == 200
    assert operation.json()["schema_version"] == "1.0"
    assert operation.json()["sequence_range"] == {"first": 1, "last": 2}
    assert operation.json()["operation_timeout_seconds"] == 900
    assert operation.json()["metadata"] == {"phase": "follow-up"}
    operation_steps = await client.get(
        f"/api/v1/executions/{execution_id}/operations/{operation_id}/steps"
    )
    assert [step["sequence"] for step in operation_steps.json()["items"]] == [
        1,
        2,
    ]

    async with container.session_factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution_id)
            .values(status=ExecutionStatus.WAITING_FOR_OPERATION, version=3)
        )
        await session.execute(
            update(ExecutionStepORM)
            .where(ExecutionStepORM.execution_id == execution_id)
            .values(status=StepStatus.SUCCEEDED)
        )

    finished = await client.post(
        f"/api/v1/executions/{execution_id}/finalize",
        json={
            "idempotency_key": "rest-finalize-1",
            "expected_version": 3,
            "actor": {"type": "USER", "id": "rest-user"},
        },
    )
    assert finished.status_code == 202
    assert finished.json()["state"]["status"] == "FINALIZING"
    assert finished.json()["state"]["version"] == 4


async def test_path_execution_spec_rest_submit(
    rest_client: tuple[httpx.AsyncClient, ApplicationContainer],
) -> None:
    client, container = rest_client
    content = b"print('PATH source')"
    relative_path = Path("plans/path-plan/step-0.py")
    source_path = container.settings.request_storage_root / relative_path
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(content)
    payload = _submit_payload(key="rest-path-submit")
    payload["operation"]["spec"]["steps"][0]["payload"] = {
        "type": "PYTHON_EXECUTE",
        "source": {
            "type": "PATH",
            "path": relative_path.as_posix(),
            "sha256": hashlib.sha256(content).hexdigest(),
        },
    }

    submitted = await client.post("/api/v1/executions", json=payload)

    assert submitted.status_code == 202
    steps = await client.get(f"{submitted.headers['location']}/steps")
    step_summary = steps.json()["items"][0]
    assert "source" not in step_summary
    assert "result_ref" not in step_summary["result"]
    detail = await client.get(
        f"{submitted.headers['location']}/steps/{step_summary['step_id']}"
    )
    assert detail.json()["source"] == {
        "type": "PATH",
        "path": relative_path.as_posix(),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


async def test_retry_and_domain_error_mapping(
    rest_client: tuple[httpx.AsyncClient, ApplicationContainer],
) -> None:
    client, container = rest_client
    payload = _submit_payload(key="rest-retry-submit")
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
    assert (
        retried.json()["operation"]["operation_id"]
        == submitted.json()["operation"]["operation_id"]
    )
    fetched = await client.get(f"/api/v1/executions/{execution_id}")
    assert fetched.json()["retry"]["count"] == 1

    missing_execution = await client.get(f"/api/v1/executions/{uuid4()}")
    missing_artifact = await client.get(f"/api/v1/artifacts/{uuid4()}")
    assert missing_execution.status_code == 404
    assert missing_execution.json()["error"]["code"] == "EXECUTION_NOT_FOUND"
    assert missing_artifact.status_code == 404
    assert missing_artifact.json()["error"]["code"] == "ARTIFACT_NOT_FOUND"
