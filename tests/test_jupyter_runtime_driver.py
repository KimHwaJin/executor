from typing import Any

import httpx
import pytest

from executor_service.domain.enums import RuntimeAbortStatus
from executor_service.domain.runtime import RuntimeDriverError
from executor_service.infrastructure.jupyter import (
    JupyterRuntimeDriver,
    _contents_path,
)


def _resource_payload() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "process_count": 5,
        "cpu": {
            "used_cores": 0.25,
            "capacity_cores": 1.0,
            "utilization": 0.125,
            "source": "CGROUP_V2",
            "estimated": False,
            "errors": [],
        },
        "memory": {
            "used_bytes": 256,
            "capacity_bytes": 1024,
            "utilization": 0.25,
            "source": "CGROUP_V2",
            "estimated": False,
            "errors": [],
        },
        "observed_at": "2026-08-12T10:00:00Z",
    }


async def test_resource_status_parses_versioned_jupyter_response() -> None:
    driver = JupyterRuntimeDriver("http://jupyter.invalid", "secret")
    await driver._client.aclose()
    driver._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=_resource_payload())
        ),
        base_url="http://jupyter.invalid",
    )
    try:
        observation = await driver.resource_status()
    finally:
        await driver.close()

    assert observation.process_count == 5
    assert observation.cpu.utilization == 0.125
    assert observation.memory.used == 256
    assert observation.observed_at.tzinfo is not None


async def test_status_reads_active_session_count_from_jupyter() -> None:
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        return httpx.Response(200, json={"kernels": 2})

    driver = JupyterRuntimeDriver("http://jupyter.invalid", "secret")
    await driver._client.aclose()
    driver._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://jupyter.invalid",
    )
    try:
        status = await driver.status()
    finally:
        await driver.close()

    assert status == {"active_session_count": 2}
    assert requested_paths == ["/api/status"]


@pytest.mark.parametrize("invalid_count", ["invalid", -1, True, None])
async def test_status_rejects_invalid_active_session_count(
    invalid_count: object,
) -> None:
    driver = JupyterRuntimeDriver("http://jupyter.invalid", "secret")
    await driver._client.aclose()
    driver._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, json={"kernels": invalid_count}
            )
        ),
        base_url="http://jupyter.invalid",
    )
    try:
        with pytest.raises(
            RuntimeDriverError, match="status response is invalid"
        ):
            await driver.status()
    finally:
        await driver.close()


async def test_abort_session_interrupts_and_confirms_idle() -> None:
    requested: list[tuple[str, str]] = []
    states = iter(["busy", "idle"])

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(204)
        return httpx.Response(200, json={"execution_state": next(states)})

    driver = JupyterRuntimeDriver("http://jupyter.invalid", "secret")
    await driver._client.aclose()
    driver._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://jupyter.invalid",
    )
    try:
        result = await driver.abort_session("kernel-1", 1)
    finally:
        await driver.close()

    assert result.status == RuntimeAbortStatus.IDLE_CONFIRMED
    assert requested == [
        ("POST", "/api/kernels/kernel-1/interrupt"),
        ("GET", "/api/kernels/kernel-1"),
        ("GET", "/api/kernels/kernel-1"),
    ]


async def test_abort_session_reports_missing_kernel() -> None:
    driver = JupyterRuntimeDriver("http://jupyter.invalid", "secret")
    await driver._client.aclose()
    driver._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(404)),
        base_url="http://jupyter.invalid",
    )
    try:
        result = await driver.abort_session("missing", 1)
    finally:
        await driver.close()

    assert result.status == RuntimeAbortStatus.SESSION_MISSING


async def test_abort_session_has_bounded_idle_confirmation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(204)
        return httpx.Response(200, json={"execution_state": "busy"})

    driver = JupyterRuntimeDriver("http://jupyter.invalid", "secret")
    await driver._client.aclose()
    driver._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://jupyter.invalid",
    )
    try:
        result = await driver.abort_session("busy", 0.01)
    finally:
        await driver.close()

    assert result.status == RuntimeAbortStatus.FAILED
    assert result.message is not None
    assert "idle" in result.message


async def test_resource_status_rejects_unknown_schema_version() -> None:
    payload = _resource_payload()
    payload["schema_version"] = "2.0"
    driver = JupyterRuntimeDriver("http://jupyter.invalid", "secret")
    await driver._client.aclose()
    driver._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=payload)
        ),
        base_url="http://jupyter.invalid",
    )
    try:
        with pytest.raises(RuntimeDriverError, match="response is invalid"):
            await driver.resource_status()
    finally:
        await driver.close()


async def test_runtime_storage_contract_uses_jupyter_server_apis() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/artifacts/snapshot"):
            return httpx.Response(
                200,
                json={
                    "files": [
                        {
                            "path": "users/u/executions/e/artifacts/plots/a.png",
                            "size_bytes": 10,
                            "modified_ns": 20,
                        }
                    ],
                    "manifest_size": 30,
                },
            )
        if request.url.path.endswith("/files/metadata"):
            return httpx.Response(
                200,
                json={
                    "path": "users/u/executions/e/artifacts/plots/a.png",
                    "name": "a.png",
                    "size_bytes": 10,
                    "modified_ns": 20,
                    "media_type": "image/png",
                    "checksum_sha256": "a" * 64,
                },
            )
        if request.url.path.endswith("/manifests/read"):
            return httpx.Response(
                200, json={"start": 0, "end": 3, "content": "{}\n"}
            )
        if request.method == "GET" and "/api/contents/" in request.url.path:
            return httpx.Response(
                200, json={"type": "notebook", "content": {"cells": []}}
            )
        return httpx.Response(200, json={})

    driver = JupyterRuntimeDriver("http://jupyter.invalid", "secret")
    await driver._client.aclose()
    driver._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://jupyter.invalid",
    )
    try:
        await driver.prepare_workspace("users/u/executions/e")
        snapshot = await driver.artifact_snapshot("users/u/executions/e")
        metadata = await driver.file_metadata(snapshot.files[0].path)
        manifest = await driver.read_manifest("users/u/executions/e", 0)
        await driver.write_notebook(
            "users/u/executions/e/notebooks/execution.ipynb", {"cells": []}
        )
        await driver.write_text(
            "users/u/executions/e/reports/final-report.md", "# Report"
        )
        notebook = await driver.read_notebook(
            "users/u/executions/e/notebooks/execution.ipynb"
        )
    finally:
        await driver.close()

    assert snapshot.manifest_size == 30
    assert metadata.checksum_sha256 == "a" * 64
    assert manifest == b"{}\n"
    assert notebook == {"cells": []}
    text_request = next(
        request
        for request in requests
        if request.url.path.endswith("/reports/final-report.md")
    )
    assert text_request.method == "PUT"
    assert (
        text_request.content
        == b'{"type":"file","format":"text","content":"# Report"}'
    )
    assert requests[0].url.path == "/executor/storage/workspaces/prepare"
    assert requests[-1].url.path.endswith(
        "/api/contents/users/u/executions/e/notebooks/execution.ipynb"
    )


async def test_request_reports_safe_http_failure_context() -> None:
    driver = JupyterRuntimeDriver("http://jupyter.invalid", "secret")
    await driver._client.aclose()
    driver._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                500, text="sensitive Jupyter response"
            )
        ),
        base_url="http://jupyter.invalid",
    )
    try:
        with pytest.raises(RuntimeDriverError) as error:
            await driver.write_notebook(
                "users/u/executions/e/notebooks/execution.ipynb", {"cells": []}
            )
    finally:
        await driver.close()

    message = str(error.value)
    assert message == (
        "Jupyter REST request failed: method=PUT "
        "path=/api/contents/users/u/executions/e/notebooks/execution.ipynb status=500."
    )
    assert "sensitive" not in message
    assert "secret" not in message


async def test_request_reports_safe_transport_failure_context() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sensitive endpoint detail", request=request)

    driver = JupyterRuntimeDriver("http://jupyter.invalid", "secret")
    await driver._client.aclose()
    driver._client = httpx.AsyncClient(
        transport=httpx.MockTransport(fail), base_url="http://jupyter.invalid"
    )
    try:
        with pytest.raises(RuntimeDriverError) as error:
            await driver.status()
    finally:
        await driver.close()

    message = str(error.value)
    assert message == (
        "Jupyter REST request failed: method=GET path=/api/status transport=ConnectError."
    )
    assert "sensitive" not in message
    assert "secret" not in message


def test_contents_path_rejects_absolute_and_parent_paths() -> None:
    assert (
        _contents_path("users/u/notebooks/a b.ipynb")
        == "users/u/notebooks/a%20b.ipynb"
    )
    with pytest.raises(RuntimeDriverError):
        _contents_path("../escape.ipynb")
    with pytest.raises(RuntimeDriverError):
        _contents_path("/absolute.ipynb")
