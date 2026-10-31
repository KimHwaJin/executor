"""Request body cap, actor id normalization and bounded mappings."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from executor_service.container import ApplicationContainer
from executor_service.interfaces.contracts.common import ActorInput
from executor_service.interfaces.execution_specs import StepLineage
from executor_service.interfaces.http.app import create_app
from executor_service.interfaces.http.body_limit import (
    RequestBodyLimitMiddleware,
)
from executor_service.interfaces.http.schemas import (
    ExecutionOperationCreateRequest,
)
from executor_service.settings import Settings


def _client(tmp_path: Path, max_bytes: int) -> TestClient:
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        redis_url="redis://localhost:6399/15",
        runtime_enabled=False,
        shared_storage_root=tmp_path,
        request_body_max_bytes=max_bytes,
    )
    return TestClient(create_app(ApplicationContainer(settings)))


def test_oversized_body_is_refused_with_413(tmp_path: Path) -> None:
    client = _client(tmp_path, 2048)
    response = client.post(
        "/api/v1/executions",
        content=b"{" + b" " * 4096 + b"}",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json() == {
        "error": {
            "code": "REQUEST_BODY_TOO_LARGE",
            "message": "Request body exceeds 2048 bytes.",
            "details": [
                {
                    "location": ["body"],
                    "message": "Request body exceeds 2048 bytes.",
                    "type": "body_too_large",
                    "context": {"limit_bytes": 2048},
                }
            ],
        }
    }


def test_body_without_length_is_counted_while_read() -> None:
    async def echo(request: Request) -> PlainTextResponse:
        return PlainTextResponse(str(len(await request.body())))

    app = Starlette(routes=[Route("/", echo, methods=["POST"])])
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=2048)
    client = TestClient(app)
    chunks = [b"x" * 1024] * 4
    response = client.post("/", content=iter(chunks))
    assert response.status_code == 413
    assert client.post("/", content=iter(chunks[:1])).text == "1024"


def test_body_under_the_limit_reaches_the_application(tmp_path: Path) -> None:
    client = _client(tmp_path, 2048)
    response = client.post(
        "/api/v1/executions",
        content=b"{}",
        headers={"content-type": "application/json"},
    )
    assert response.status_code != 413


def test_actor_id_is_stored_lowercase() -> None:
    assert ActorInput(type="USER", id=" Alice.Kim ").id == "alice.kim"
    with pytest.raises(ValidationError):
        ActorInput(type="USER", id="   ")


def test_operation_metadata_and_input_parameters_are_bounded() -> None:
    with pytest.raises(ValidationError):
        StepLineage(input_parameters={f"p{i}": i for i in range(33)})
    with pytest.raises(ValidationError):
        StepLineage(input_parameters={"query": "x" * 5000})
    # Tool parameter names keep the caller's spelling.
    assert StepLineage(input_parameters={"tableName": "t"}).input_parameters
    base = {
        "idempotency_key": "op-1",
        "expected_version": 1,
        "spec": {
            "schema_version": "1.0",
            "steps": [
                {
                    "sequence": 1,
                    "payload": {
                        "type": "PYTHON_EXECUTE",
                        "source": {"type": "INLINE", "content": "x = 1"},
                    },
                }
            ],
        },
        "actor": {"type": "AGENT", "id": "agent"},
    }
    with pytest.raises(ValidationError):
        ExecutionOperationCreateRequest.model_validate(
            {**base, "metadata": {"BadKey": 1}}
        )
    with pytest.raises(ValidationError):
        ExecutionOperationCreateRequest.model_validate(
            {**base, "metadata": {"note": "x" * 5000}}
        )
    assert ExecutionOperationCreateRequest.model_validate(
        {**base, "metadata": {"run_id": "r1"}}
    ).metadata == {"run_id": "r1"}
