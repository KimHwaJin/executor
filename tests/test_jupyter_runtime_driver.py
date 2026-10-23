import errno
import json
from typing import Any
from uuid import UUID

import httpx
import pytest
from websockets.datastructures import Headers
from websockets.exceptions import (
    ConnectionClosedError,
    InvalidStatus,
    PayloadTooBig,
)
from websockets.frames import Close
from websockets.http11 import Response

from executor_service.domain.enums import RuntimeAbortStatus
from executor_service.domain.runtime import (
    RuntimeDriverError,
    RuntimeExecutionError,
    RuntimeNotebookSourceCell,
    RuntimeOutputLimitExceededError,
    RuntimeOutputRecord,
    RuntimeSessionLostError,
)
from executor_service.infrastructure.jupyter import (
    JupyterRuntimeDriver,
    _as_output_record,
    _contents_path,
)

_CONNECT = (
    "executor_service.infrastructure.jupyter.execution.websockets.connect"
)
_REQUEST_ID = UUID("00000000-0000-4000-8000-000000000001")


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


@pytest.mark.parametrize("source", ["CGROUP_V1", "CGROUP_V2"])
async def test_resource_status_parses_versioned_jupyter_response(
    source: str,
) -> None:
    payload = _resource_payload()
    payload["cpu"]["source"] = source
    payload["memory"]["source"] = source
    payload["memory"]["estimated"] = source == "CGROUP_V1"
    driver = JupyterRuntimeDriver("http://jupyter.invalid", "secret")
    await driver._client.aclose()
    driver._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=payload)
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
    assert observation.cpu.source == source
    assert observation.memory.source == source
    assert observation.memory.estimated == (source == "CGROUP_V1")
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
        if request.url.path.endswith("/files/content"):
            return httpx.Response(
                206,
                content=b"2345",
                headers={
                    "Content-Range": "bytes 2-5/10",
                    "X-Checksum-SHA256": "a" * 64,
                    "ETag": '"' + "a" * 64 + '"',
                },
            )
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
        if request.url.path.endswith("/notebooks/project"):
            return httpx.Response(
                200,
                json={
                    "notebook_path": (
                        "users/u/executions/e/notebooks/execution.ipynb"
                    ),
                    "cell_count": 0,
                    "checksum_sha256": "b" * 64,
                },
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
        async with driver.open_file(
            "users/u/executions/e/artifacts/plots/a.png", "bytes=2-5"
        ) as opened:
            streamed = b"".join([chunk async for chunk in opened.body])
    finally:
        await driver.close()

    assert snapshot.manifest_size == 30
    assert metadata.checksum_sha256 == "a" * 64
    assert manifest == b"{}\n"
    assert notebook == {"cells": []}
    assert streamed == b"2345"
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
    notebook_request = next(
        request
        for request in requests
        if request.url.path.endswith("/notebooks/project")
    )
    assert notebook_request.method == "POST"
    content_request = requests[-1]
    assert content_request.url.path.endswith("/storage/files/content")
    assert dict(content_request.url.params) == {
        "path": "users/u/executions/e/artifacts/plots/a.png",
    }
    assert content_request.headers["Range"] == "bytes=2-5"


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
        "Jupyter REST request failed: method=POST "
        "path=/executor/storage/notebooks/project status=500."
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
    workspace_path = "users/u/projects/p/sessions/s/executions/e"
    execution_id = UUID("11111111-1111-4111-8111-111111111111")
    operation_id = UUID("22222222-2222-4222-8222-222222222222")
    step_id = UUID("33333333-3333-4333-8333-333333333333")
    try:
        result = await driver.prepare_notebook(
            workspace_path,
            execution_id,
            "basic",
            (
                RuntimeNotebookSourceCell(
                    sequence=0,
                    operation_id=operation_id,
                    step_id=step_id,
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
    assert payload["execution_id"] == str(execution_id)
    assert payload["cells"] == [
        {
            "sequence": 0,
            "operation_id": str(operation_id),
            "step_id": str(step_id),
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


def _message(seq: int, channel: str, msg_type: str, content: Any) -> str:
    return json.dumps(
        {
            "seq": seq,
            "type": "message",
            "channel": channel,
            "msg_type": msg_type,
            "content": content,
        }
    )


def _terminal(seq: int, state: str, message: str | None = None) -> str:
    return json.dumps(
        {"seq": seq, "type": "terminal", "state": state, "message": message}
    )


def _caught_up(seq: int, *, finished: bool) -> str:
    return json.dumps({"type": "caught_up", "seq": seq, "finished": finished})


class FakeStream:
    """One journal stream connection: frames, then an optional failure."""

    def __init__(
        self, frames: list[str], error: BaseException | None = None
    ) -> None:
        self._frames = frames
        self._error = error

    async def __aenter__(self) -> "FakeStream":
        return self

    async def __aexit__(self, *_args: object) -> None:
        pass

    def __aiter__(self) -> "FakeStream":
        return self

    async def __anext__(self) -> str:
        if self._frames:
            return self._frames.pop(0)
        if self._error is not None:
            raise self._error
        raise StopAsyncIteration


def _connect_sequence(
    monkeypatch: pytest.MonkeyPatch,
    *streams: FakeStream | BaseException,
) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []
    pending = list(streams)

    def connect(uri: str, **kwargs: Any) -> FakeStream:
        calls.append((uri, kwargs))
        stream = pending.pop(0)
        if isinstance(stream, BaseException):
            raise stream
        return stream

    monkeypatch.setattr(_CONNECT, connect)
    monkeypatch.setattr(
        "executor_service.infrastructure.jupyter.execution."
        "_RECONNECT_DELAY_SECONDS",
        0,
    )
    return calls


async def _follow(
    driver: JupyterRuntimeDriver,
    records: list[RuntimeOutputRecord] | None = None,
    caught_up: list[bool] | None = None,
) -> Any:
    async def collect(record: RuntimeOutputRecord) -> None:
        if records is not None:
            records.append(record)

    async def on_caught_up(finished: bool) -> None:
        if caught_up is not None:
            caught_up.append(finished)

    try:
        return await driver.follow_execution(
            "kernel-1", _REQUEST_ID, collect, on_caught_up
        )
    finally:
        await driver.close()


async def test_start_execution_submits_once_per_request_id() -> None:
    requests: list[httpx.Request] = []
    statuses = iter([201, 200, 404])

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(next(statuses), json={})

    driver = JupyterRuntimeDriver("http://jupyter.invalid", "secret")
    await driver._client.aclose()
    driver._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://jupyter.invalid",
    )
    try:
        await driver.start_execution("kernel-1", _REQUEST_ID, "print(1)")
        await driver.start_execution("kernel-1", _REQUEST_ID, "print(1)")
        with pytest.raises(RuntimeSessionLostError):
            await driver.start_execution("kernel-1", _REQUEST_ID, "x")
    finally:
        await driver.close()

    assert requests[0].url.path == "/executor/kernels/kernel-1/executions"
    assert json.loads(requests[0].content) == {
        "request_id": str(_REQUEST_ID),
        "code": "print(1)",
    }


async def test_start_execution_resubmits_after_a_lost_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout("lost response", request=request)
        return httpx.Response(200, json={"created": False})

    driver = JupyterRuntimeDriver("http://jupyter.invalid", "secret")
    await driver._client.aclose()
    driver._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://jupyter.invalid",
    )
    monkeypatch.setattr(
        "executor_service.infrastructure.jupyter.execution."
        "_RECONNECT_DELAY_SECONDS",
        0,
    )
    try:
        await driver.start_execution("kernel-1", _REQUEST_ID, "print(1)")
    finally:
        await driver.close()

    assert attempts == 2


@pytest.mark.parametrize(
    "output_error",
    [
        None,
        PermissionError(errno.EACCES, "permission denied"),
        TimeoutError("storage timeout"),
    ],
)
async def test_follow_delivers_each_journal_output(
    monkeypatch: pytest.MonkeyPatch,
    output_error: Exception | None,
) -> None:
    calls = _connect_sequence(
        monkeypatch,
        FakeStream(
            [
                _message(
                    1, "iopub", "stream", {"name": "stdout", "text": "one\n"}
                ),
                _caught_up(1, finished=False),
                _message(
                    2,
                    "shell",
                    "execute_reply",
                    {"status": "ok", "execution_count": 3},
                ),
                _terminal(3, "COMPLETED"),
            ]
        ),
    )
    records: list[RuntimeOutputRecord] = []

    async def collect(record: RuntimeOutputRecord) -> None:
        if output_error is not None:
            raise output_error
        records.append(record)

    driver = JupyterRuntimeDriver(
        "https://jupyter.invalid/base",
        "secret",
        max_output_message_bytes=1024,
    )
    try:
        if output_error is not None:
            with pytest.raises(type(output_error)) as raised:
                await driver.follow_execution("kernel-1", _REQUEST_ID, collect)
            assert raised.value is output_error
            return
        result = await driver.follow_execution(
            "kernel-1", _REQUEST_ID, collect
        )
    finally:
        await driver.close()

    assert result.execution_count == 3
    assert [record.kind for record in records] == ["STREAM"]
    uri, options = calls[0]
    assert uri == (
        "wss://jupyter.invalid/base/executor/kernels/kernel-1/executions/"
        f"{_REQUEST_ID}/stream?after=0"
    )
    assert options["max_size"] == 1024
    assert options["additional_headers"] == {"Authorization": "token secret"}


async def test_follow_resumes_after_the_last_entry_without_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _connect_sequence(
        monkeypatch,
        FakeStream(
            [
                _message(
                    1, "iopub", "stream", {"name": "stdout", "text": "a"}
                ),
                _caught_up(1, finished=False),
            ],
            ConnectionClosedError(Close(1011, "gone"), None),
        ),
        OSError("connection refused"),
        FakeStream(
            [
                _message(
                    2, "iopub", "stream", {"name": "stdout", "text": "b"}
                ),
                _caught_up(2, finished=True),
                _terminal(3, "COMPLETED"),
            ]
        ),
    )
    records: list[RuntimeOutputRecord] = []
    caught_up: list[bool] = []

    await _follow(
        JupyterRuntimeDriver("http://jupyter.invalid", "secret"),
        records,
        caught_up,
    )

    assert [uri.rsplit("=", 1)[1] for uri, _ in calls] == ["0", "1", "1"]
    assert [record.representations[0].content for record in records] == [
        "a",
        "b",
    ]
    assert caught_up == [False]


async def test_a_journal_that_already_finished_reports_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect_sequence(
        monkeypatch,
        FakeStream(
            [
                _message(
                    1, "iopub", "stream", {"name": "stdout", "text": "a"}
                ),
                _terminal(2, "COMPLETED"),
                _caught_up(2, finished=True),
            ]
        ),
    )
    caught_up: list[bool] = []

    await _follow(
        JupyterRuntimeDriver("http://jupyter.invalid", "secret"),
        caught_up=caught_up,
    )

    assert caught_up == [True]


async def test_follow_rejects_a_gap_in_the_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect_sequence(
        monkeypatch,
        FakeStream([_message(2, "iopub", "stream", {"text": "late"})]),
    )

    with pytest.raises(RuntimeDriverError, match="gap"):
        await _follow(JupyterRuntimeDriver("http://jupyter.invalid", "secret"))


async def test_follow_gives_up_after_the_reconnect_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect_sequence(monkeypatch, OSError("down"), OSError("down"))
    driver = JupyterRuntimeDriver(
        "http://jupyter.invalid", "secret", stream_reconnect_seconds=0.001
    )
    monkeypatch.setattr(
        "executor_service.infrastructure.jupyter.execution."
        "_RECONNECT_DELAY_SECONDS",
        0.01,
    )

    with pytest.raises(RuntimeDriverError, match="stayed unavailable"):
        await _follow(driver)


@pytest.mark.parametrize(
    "stream",
    [
        FakeStream([_terminal(1, "LOST", "The kernel process stopped.")]),
        InvalidStatus(Response(404, "Not Found", Headers())),
    ],
)
async def test_lost_or_unknown_execution_reports_a_lost_session(
    monkeypatch: pytest.MonkeyPatch,
    stream: FakeStream | BaseException,
) -> None:
    _connect_sequence(monkeypatch, stream)

    with pytest.raises(RuntimeSessionLostError):
        await _follow(JupyterRuntimeDriver("http://jupyter.invalid", "secret"))


async def test_failed_code_reports_its_error_after_delivering_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = {"ename": "ValueError", "evalue": "bad", "traceback": ["tb"]}
    _connect_sequence(
        monkeypatch,
        FakeStream(
            [
                _message(1, "iopub", "error", error),
                _message(
                    2, "shell", "execute_reply", {"status": "error", **error}
                ),
                _terminal(3, "COMPLETED"),
            ]
        ),
    )
    records: list[RuntimeOutputRecord] = []

    with pytest.raises(RuntimeExecutionError, match="ValueError: bad"):
        await _follow(
            JupyterRuntimeDriver("http://jupyter.invalid", "secret"), records
        )

    assert [record.kind for record in records] == ["ERROR"]


@pytest.mark.parametrize(
    ("stream", "kind"),
    [
        (FakeStream([], PayloadTooBig(2048, 1024)), "MESSAGE_SIZE"),
        (
            FakeStream(
                [], ConnectionClosedError(None, Close(1009, "message too big"))
            ),
            "MESSAGE_SIZE",
        ),
        (
            FakeStream([_terminal(1, "OUTPUT_LIMIT", "Output too large.")]),
            "JOURNAL_SIZE",
        ),
    ],
)
async def test_output_limits_end_the_step_as_incomplete(
    monkeypatch: pytest.MonkeyPatch,
    stream: FakeStream,
    kind: str,
) -> None:
    _connect_sequence(monkeypatch, stream)

    with pytest.raises(RuntimeOutputLimitExceededError) as raised:
        await _follow(
            JupyterRuntimeDriver(
                "http://jupyter.invalid",
                "secret",
                max_output_message_bytes=1024,
            )
        )

    assert raised.value.kind == kind
