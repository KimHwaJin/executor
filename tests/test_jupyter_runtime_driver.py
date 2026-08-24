import json
from typing import Any
from uuid import UUID

import httpx
import pytest

from executor_service.domain.enums import RuntimeAbortStatus
from executor_service.domain.runtime import (
    RuntimeDriverError,
    RuntimeNotebookCell,
    RuntimeNotebookSourceCell,
    RuntimeOutputJournalIdentity,
    RuntimeOutputRecord,
)
from executor_service.infrastructure.jupyter import (
    JupyterRuntimeDriver,
    _as_output_record,
    _contents_path,
    _deserialize_v1,
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


def _journal_identity() -> RuntimeOutputJournalIdentity:
    return RuntimeOutputJournalIdentity(
        workspace_path="users/u/projects/p/sessions/s/executions/e",
        execution_id=UUID("11111111-1111-4111-8111-111111111111"),
        operation_id=UUID("22222222-2222-4222-8222-222222222222"),
        step_id=UUID("33333333-3333-4333-8333-333333333333"),
        sequence=0,
        execution_attempt_id=UUID("44444444-4444-4444-8444-444444444444"),
        fencing_token=7,
        runtime_target_id=UUID("55555555-5555-4555-8555-555555555555"),
        runtime_session_id="kernel-1",
    )


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


async def test_output_journal_contract_uses_authenticated_extension_apis() -> (
    None
):
    requests: list[httpx.Request] = []
    journal_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    batch_id = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    output_id = UUID("11111111-1111-4111-8111-111111111111")
    representation_id = UUID("22222222-2222-4222-8222-222222222222")

    def descriptor(state: str, offset: int) -> dict[str, Any]:
        return {
            "journal_id": journal_id,
            "state": state,
            "committed_offset": offset,
            "output_count": offset,
            "representation_count": offset,
            "total_bytes": 4 * offset,
            "checksum_sha256": "c" * 64 if state == "FINALIZED" else None,
        }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/read"):
            return httpx.Response(
                206,
                content=b"on",
                headers={
                    "Content-Type": "text/plain",
                    "X-Content-Size": "4",
                    "X-Checksum-SHA256": "a" * 64,
                    "X-Content-Complete": "true",
                    "X-Content-Start": "1",
                    "X-Content-End-Exclusive": "3",
                },
            )
        if request.url.path.endswith("/append"):
            return httpx.Response(
                200,
                json={
                    "journal_id": journal_id,
                    "state": "OPEN",
                    "batch_id": str(batch_id),
                    "committed_offset": 1,
                    "output_count": 1,
                    "representation_count": 1,
                    "total_bytes": 4,
                    "replayed": False,
                    "outputs": [
                        {
                            "output_id": (
                                "11111111-1111-4111-8111-111111111111"
                            ),
                            "ordinal": 0,
                            "kind": "STREAM",
                            "stream_name": "stdout",
                            "execution_count": None,
                            "representations": [
                                {
                                    "representation_id": (
                                        "22222222-2222-4222-8222-222222222222"
                                    ),
                                    "media_type": "text/plain",
                                    "size_bytes": 4,
                                    "checksum_sha256": "a" * 64,
                                    "complete": True,
                                    "content_ref": (
                                        "journal://aaaaaaaa-aaaa-4aaa-8aaa-"
                                        "aaaaaaaaaaaa/output/representation"
                                    ),
                                    "metadata": {},
                                }
                            ],
                            "metadata": {},
                            "created_at": "2026-08-24T00:00:00+00:00",
                        }
                    ],
                },
            )
        if request.url.path.endswith("/finalize"):
            return httpx.Response(200, json=descriptor("FINALIZED", 1))
        if request.url.path.endswith("/abort"):
            return httpx.Response(200, json=descriptor("ABORTED", 1))
        return httpx.Response(200, json=descriptor("OPEN", 0))

    driver = JupyterRuntimeDriver("http://jupyter.invalid", "secret")
    await driver._client.aclose()
    driver._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://jupyter.invalid",
        headers={"Authorization": "token secret"},
    )
    identity = _journal_identity()
    record = _as_output_record("stream", {"name": "stdout", "text": "done"})
    assert record is not None
    try:
        begun = await driver.output_journal_begin(identity, "print('done')")
        appended = await driver.output_journal_append(
            identity,
            journal_id=begun.journal_id,
            expected_offset=0,
            batch_id=batch_id,
            records=(record,),
        )
        finalized = await driver.output_journal_finalize(
            identity, journal_id=begun.journal_id
        )
        aborted = await driver.output_journal_abort(
            identity,
            journal_id=begun.journal_id,
            reason="incomplete",
        )
        content = await driver.output_journal_read(
            identity,
            journal_id=begun.journal_id,
            output_id=output_id,
            representation_id=representation_id,
            start=1,
            end_exclusive=3,
        )
        streamed = b"".join(
            [
                chunk
                async for chunk in driver.output_journal_stream(
                    identity,
                    journal_id=begun.journal_id,
                    output_id=output_id,
                    representation_id=representation_id,
                    start=1,
                    end_exclusive=3,
                    expected_media_type="text/plain",
                    expected_size_bytes=4,
                    expected_checksum_sha256="a" * 64,
                    expected_complete=True,
                )
            ]
        )
    finally:
        await driver.close()

    assert begun.state == "OPEN"
    assert appended.committed_offset == 1
    assert finalized.state == "FINALIZED"
    assert aborted.state == "ABORTED"
    assert content.content == b"on"
    assert streamed == b"on"
    assert content.size_bytes == 4
    assert [request.url.path.rsplit("/", 1)[-1] for request in requests] == [
        "begin",
        "append",
        "finalize",
        "abort",
        "read",
        "read",
    ]
    append_payload = json.loads(requests[1].content)
    begin_payload = json.loads(requests[0].content)
    assert begin_payload["source"] == "print('done')"
    assert begin_payload["journal"]["fencing_token"] == 7
    assert append_payload["journal"]["fencing_token"] == 7
    assert append_payload["records"] == [
        {
            "kind": "STREAM",
            "stream_name": "stdout",
            "execution_count": None,
            "representations": [
                {
                    "media_type": "text/plain",
                    "encoding": "UTF8",
                    "content": "done",
                    "metadata": {},
                }
            ],
            "metadata": {},
        }
    ]


async def test_jupyter_materializes_notebook_from_output_journals() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "notebook_path": (
                    "users/u/projects/p/sessions/s/executions/e/"
                    "notebooks/execution.ipynb"
                ),
                "cell_count": 1,
                "output_count": 2,
            },
        )

    driver = JupyterRuntimeDriver("http://jupyter.invalid", "secret")
    await driver._client.aclose()
    driver._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://jupyter.invalid",
        headers={"Authorization": "token secret"},
    )
    identity = _journal_identity()
    try:
        result = await driver.materialize_notebook(
            identity.workspace_path,
            "basic",
            (
                RuntimeNotebookCell(
                    sequence=0,
                    execution_count=1,
                    journal_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
                    journal=identity,
                ),
            ),
        )
    finally:
        await driver.close()

    assert result.cell_count == 1
    assert result.output_count == 2
    assert requests[0].url.path.endswith(
        "/output-journals/materialize-notebook"
    )
    payload = json.loads(requests[0].content)
    assert "source" not in payload["cells"][0]
    assert payload["cells"][0]["journal"]["fencing_token"] == 7
    assert requests[0].headers["authorization"] == "token secret"


async def test_jupyter_prepares_notebook_source_cells_before_execution() -> (
    None
):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "notebook_path": (
                    "users/u/projects/p/sessions/s/executions/e/"
                    "notebooks/execution.ipynb"
                ),
                "prepared_cell_count": 1,
                "total_cell_count": 1,
            },
        )

    driver = JupyterRuntimeDriver("http://jupyter.invalid", "secret")
    await driver._client.aclose()
    driver._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://jupyter.invalid",
        headers={"Authorization": "token secret"},
    )
    identity = _journal_identity()
    try:
        result = await driver.prepare_notebook(
            identity.workspace_path,
            identity.execution_id,
            "basic",
            (
                RuntimeNotebookSourceCell(
                    sequence=0,
                    operation_id=identity.operation_id,
                    step_id=identity.step_id,
                    source="print('prepared')",
                ),
            ),
        )
    finally:
        await driver.close()

    assert result.prepared_cell_count == 1
    assert result.total_cell_count == 1
    assert requests[0].url.path.endswith("/storage/notebooks/prepare")
    payload = json.loads(requests[0].content)
    assert payload["execution_id"] == str(identity.execution_id)
    assert payload["cells"] == [
        {
            "sequence": 0,
            "operation_id": str(identity.operation_id),
            "step_id": str(identity.step_id),
            "source": "print('prepared')",
        }
    ]


def test_output_record_mapping_preserves_jupyter_mime_semantics() -> None:
    display = _as_output_record(
        "display_data",
        {
            "data": {
                "text/plain": "chart",
                "application/json": {"score": 0.9},
                "image/png": "cG5n",
                "image/gif": "Z2lm",
                "image/svg+xml": "<svg></svg>",
            },
            "metadata": {"width": 10},
            "transient": {"display_id": "display-1"},
        },
    )
    error = _as_output_record(
        "error",
        {
            "ename": "ValueError",
            "evalue": "bad value",
            "traceback": ["line 1", "line 2"],
        },
    )

    assert display is not None
    assert display.kind == "DISPLAY"
    assert [item.encoding for item in display.representations] == [
        "UTF8",
        "UTF8",
        "BASE64",
        "BASE64",
        "UTF8",
    ]
    assert display.representations[1].content == '{"score":0.9}'
    assert display.metadata["transient"] == {"display_id": "display-1"}
    assert error is not None
    assert error.kind == "ERROR"
    assert error.representations[0].content == "line 1\nline 2"


async def test_execute_streaming_delivers_each_iopub_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeWebSocket:
        def __init__(self) -> None:
            self.message_id = ""
            self.messages = [
                ("iopub", "stream", {"name": "stdout", "text": "one\n"}),
                (
                    "shell",
                    "execute_reply",
                    {"status": "ok", "execution_count": 3},
                ),
                ("iopub", "status", {"execution_state": "idle"}),
            ]

        async def __aenter__(self) -> "FakeWebSocket":
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        async def send(self, raw: bytes) -> None:
            _, message = _deserialize_v1(raw)
            self.message_id = str(message["header"]["msg_id"])

        async def recv(self) -> str:
            channel, msg_type, content = self.messages.pop(0)
            return json.dumps(
                {
                    "channel": channel,
                    "header": {"msg_type": msg_type},
                    "parent_header": {"msg_id": self.message_id},
                    "metadata": {},
                    "content": content,
                }
            )

    socket = FakeWebSocket()
    monkeypatch.setattr(
        "executor_service.infrastructure.jupyter.websockets.connect",
        lambda *_args, **_kwargs: socket,
    )
    records: list[RuntimeOutputRecord] = []

    async def collect(record: RuntimeOutputRecord) -> None:
        records.append(record)

    driver = JupyterRuntimeDriver("http://jupyter.invalid", "secret")
    try:
        result = await driver.execute_streaming(
            "kernel-1", "print('one')", collect
        )
    finally:
        await driver.close()

    assert result.execution_count == 3
    assert result.outputs == [
        {"output_type": "stream", "name": "stdout", "text": "one\n"}
    ]
    assert len(records) == 1
    assert records[0].kind == "STREAM"
