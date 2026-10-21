import json
import logging
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from starlette.types import Message

from executor_service.interfaces.http.request_error_logging import (
    ExecutionRequestErrorLoggingMiddleware,
)
from tests.test_rest_execution_api import rest_client as rest_client

LOGGER = "executor_service.interfaces.http.request_error_logging"


@pytest.fixture(autouse=True)
def enable_test_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REST_ERROR_BODY_LOGGING_ENABLED", "true")


def records(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == LOGGER
    ]


@pytest.mark.parametrize("body", ['{"runtime":', '{"password":"example"}'])
async def test_real_execution_validation_logs_body(
    rest_client, caplog: pytest.LogCaptureFixture, body: str
) -> None:
    client, _ = rest_client
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        response = await client.post(
            "/api/v1/executions",
            content=body,
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 422
    (entry,) = records(caplog)
    assert entry["request_body"] == body
    assert entry["body_complete"] is True
    assert entry["body_truncated"] is False
    assert json.loads(entry["error_response"]) == response.json()


@pytest.mark.parametrize(
    ("path", "failure", "enabled", "expected"),
    [
        ("/api/v1/executions", "conflict", True, 409),
        ("/api/v1/executions/id/cancel", "crash", True, 500),
        ("/api/v1/executions", "ok", True, None),
        ("/api/v1/executions", "conflict", False, None),
        ("/mcp", "conflict", True, None),
        ("/api/v1/runtime-targets", "conflict", True, None),
    ],
)
async def test_error_logging_scope_and_response_preserved(
    path: str,
    failure: str,
    enabled: bool,
    expected: int | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = FastAPI()
    app.add_middleware(
        ExecutionRequestErrorLoggingMiddleware,
        enabled=enabled,
        max_body_bytes=65536,
    )

    @app.post(path)
    async def endpoint(body: dict[str, Any]) -> dict[str, Any]:
        if failure == "conflict":
            raise HTTPException(409, "conflicting request")
        if failure == "crash":
            raise RuntimeError("test failure")
        return body

    body = {"code": "print('한글')", "token": "unmasked-test-value"}
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        with caplog.at_level(logging.ERROR, logger=LOGGER):
            response = await client.post(path, json=body)
    assert (
        response.status_code
        == {"ok": 200, "conflict": 409, "crash": 500}[failure]
    )
    entries = records(caplog)
    if expected is None:
        assert entries == []
    else:
        (entry,) = entries
        assert entry["status_code"] == expected
        assert json.loads(entry["request_body"]) == body
        if failure == "crash":
            assert entry["exception_message"] == "test failure"


async def test_chunked_body_bounded_without_changing_received_bytes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    chunks = iter(
        [
            {"type": "http.request", "body": b"123", "more_body": True},
            {"type": "http.request", "body": b"456789", "more_body": False},
        ]
    )
    received = bytearray()
    sent: list[Message] = []

    async def receive() -> Message:
        return next(chunks)

    async def send(message: Message) -> None:
        sent.append(message)

    async def app(scope, receive, send) -> None:
        while True:
            message = await receive()
            received.extend(message["body"])
            if not message["more_body"]:
                break
        await send({"type": "http.response.start", "status": 422})
        await send({"type": "http.response.body", "body": b"invalid"})

    middleware = ExecutionRequestErrorLoggingMiddleware(
        app, enabled=True, max_body_bytes=5
    )
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        await middleware(
            {"type": "http", "path": "/api/v1/executions", "method": "POST"},
            receive,
            send,
        )
    assert received == b"123456789"
    assert len(sent) == 2
    (entry,) = records(caplog)
    assert entry["request_body"] == "12345"
    assert entry["body_observed_bytes"] == 9
    assert entry["body_truncated"] is True


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            '{"runtime":{"type":"JUPYTER","env":{"S3_SECRET":"s3-value"}}}',
            {"runtime": {"type": "JUPYTER", "env": {"S3_SECRET": "***"}}},
        ),
        ('{"runtime":{"env":{"S3_SECRET":"s3-val', None),
    ],
)
async def test_runtime_env_values_are_never_logged(
    rest_client,
    caplog: pytest.LogCaptureFixture,
    body: str,
    expected: dict[str, Any] | None,
) -> None:
    client, _ = rest_client
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        await client.post(
            "/api/v1/executions",
            content=body,
            headers={"Content-Type": "application/json"},
        )
    (entry,) = records(caplog)
    assert "s3-val" not in json.dumps(entry)
    if expected is not None:
        assert json.loads(entry["request_body"]) == expected
