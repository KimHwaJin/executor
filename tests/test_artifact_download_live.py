"""Opt-in real Jupyter + Executor + BFF download regression.

Run with RUN_ARTIFACT_DOWNLOAD_LIVE=1 uv run pytest -q -s
tests/test_artifact_download_live.py. Uses a disposable Docker Jupyter, an
isolated SQLite DB, and local HTTP servers; never modifies Compose services.
"""

import asyncio
import hashlib
import json
import os
import socket
import subprocess
import tempfile
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import nbformat
import pytest
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import update
from starlette.types import Receive, Scope, Send

from executor_service.container import ApplicationContainer
from executor_service.domain.enums import (
    ArtifactStatus,
    ArtifactStorageType,
    ArtifactType,
    ExecutionStatus,
    RuntimePool,
    RuntimeTargetStatus,
    RuntimeType,
)
from executor_service.infrastructure.db.models import (
    ExecutionArtifactORM,
    ExecutionORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.jupyter import JupyterRuntimeDriver
from executor_service.interfaces.http.app import create_app
from tests.runtime_credentials import runtime_credential_fields
from tests.test_rest_execution_api import _submit_payload
from tests.test_rest_execution_api import rest_client as rest_client

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_ARTIFACT_DOWNLOAD_LIVE") != "1",
    reason="Opt-in disposable Docker Jupyter download test",
)


@dataclass
class LiveJupyter:
    endpoint: str
    token: str = field(repr=False)
    root: Path


@pytest.fixture(scope="module")
def jupyter() -> Iterator[LiveJupyter]:
    repo = Path(__file__).resolve().parents[1]
    name = f"executor-download-test-{uuid4().hex[:12]}"
    token = uuid4().hex
    with tempfile.TemporaryDirectory(prefix="executor-download-") as directory:
        root = Path(directory)
        root.chmod(0o777)  # Disposable mount writable by container UID 1000.
        created = False
        try:
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--detach",
                    "--rm",
                    "--name",
                    name,
                    "--publish",
                    "127.0.0.1::8888",
                    "--env",
                    "JUPYTER_TOKEN",
                    "--env",
                    "PYTHONPATH=/opt/download-extension/src",
                    "--mount",
                    f"type=bind,src={repo / 'test_harness/jupyter/extension'},dst=/opt/download-extension,readonly",
                    "--mount",
                    f"type=bind,src={root},dst=/workspace/pv",
                    "executor-jupyter:local",
                ],
                env={**os.environ, "JUPYTER_TOKEN": token},
                check=True,
                capture_output=True,
                timeout=30,
            )
            created = True
            published = subprocess.run(
                ["docker", "port", name, "8888/tcp"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()
            endpoint = f"http://{published}"
            deadline = time.monotonic() + 60
            with httpx.Client(
                headers={"Authorization": f"token {token}"}, timeout=2
            ) as client:
                while True:
                    try:
                        if (
                            client.get(f"{endpoint}/api/status").status_code
                            == 200
                        ):
                            break
                    except httpx.HTTPError:
                        pass
                    if time.monotonic() >= deadline:
                        raise AssertionError(
                            "Disposable Jupyter did not become ready."
                        )
                    time.sleep(0.2)
            yield LiveJupyter(endpoint, token, root)
        finally:
            if created:
                logs = subprocess.run(
                    ["docker", "logs", name],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                for line in (logs.stdout + logs.stderr).splitlines():
                    if "Runtime file download setup failed:" in line:
                        print(line)
                subprocess.run(
                    ["docker", "stop", "--time", "5", name],
                    check=True,
                    capture_output=True,
                    timeout=20,
                )


@asynccontextmanager
async def serve(app: FastAPI) -> AsyncIterator[str]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="critical", lifespan="off")
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(200):
            if server.started:
                break
            if task.done():
                await task
                raise AssertionError("HTTP test server exited.")
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("HTTP test server did not start.")
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()


class ProxyResponse(StreamingResponse):
    def __init__(self, upstream: httpx.Response) -> None:
        self.upstream = upstream
        headers = {
            key: value
            for key, value in upstream.headers.items()
            if key
            in {
                "content-type",
                "content-length",
                "content-range",
                "content-disposition",
                "accept-ranges",
                "etag",
                "x-checksum-sha256",
                "cache-control",
            }
        }
        super().__init__(
            upstream.aiter_raw(),
            status_code=upstream.status_code,
            headers=headers,
        )

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.upstream.aclose()


@asynccontextmanager
async def bff(executor_url: str) -> AsyncIterator[str]:
    app = FastAPI()
    async with httpx.AsyncClient(base_url=executor_url, timeout=60) as client:

        @app.get("/api/v1/artifacts/{artifact_id}/content")
        async def download(artifact_id: str, request: Request) -> Response:
            headers = {"Accept-Encoding": "identity"}
            if "range" in request.headers:
                headers["Range"] = request.headers["range"]
            upstream = await client.send(
                client.build_request(
                    "GET",
                    f"/api/v1/artifacts/{artifact_id}/content",
                    headers=headers,
                ),
                stream=True,
            )
            return ProxyResponse(upstream)

        async with serve(app) as endpoint:
            yield endpoint


@pytest.mark.parametrize("profile", ["basic", "ml"])
async def test_real_notebook_download_after_report_and_manual_edits(
    jupyter: LiveJupyter,
    rest_client: tuple[httpx.AsyncClient, ApplicationContainer],
    profile: str,
) -> None:
    client, container = rest_client
    submitted = await client.post("/api/v1/executions", json=_submit_payload())
    assert submitted.status_code == 202, submitted.text
    execution_id = UUID(submitted.json()["execution_id"])
    workspace = f"users/download-test/projects/live/sessions/{profile}/executions/{execution_id}"
    notebook_path = f"{workspace}/notebooks/execution.ipynb"
    driver = JupyterRuntimeDriver(jupyter.endpoint, jupyter.token)
    kernel: str | None = None
    try:
        await driver.prepare_workspace(workspace)
        kernel = await driver.start_session(profile, workspace)
        notebook = nbformat.v4.new_notebook()
        for index in range(7):
            code = (
                "import matplotlib.pyplot as plt\n"
                "plt.plot([1, 2, 3], [1, 4, 2])\nplt.title('Download test')\nplt.show()"
                if index == 6
                else f"print('Cell {index}:', sum(range({index + 10})))"
            )
            result = await driver.execute(kernel, code)
            notebook.cells.append(
                nbformat.v4.new_code_cell(
                    source=code,
                    execution_count=result.execution_count,
                    outputs=[
                        nbformat.from_dict(output) for output in result.outputs
                    ],
                )
            )
        await driver.write_notebook(
            notebook_path, json.loads(nbformat.writes(notebook))
        )
        original = (jupyter.root / notebook_path).read_bytes()
        assert any(
            "image/png" in output.get("data", {})
            for cell in notebook.cells
            for output in cell.outputs
        )
        target_id, artifact_id = uuid4(), uuid4()
        async with container.session_factory() as session, session.begin():
            session.add(
                RuntimeTargetORM(
                    id=target_id,
                    name="download-target",
                    runtime_type=RuntimeType.JUPYTER,
                    pool=RuntimePool.INTERACTIVE,
                    status=RuntimeTargetStatus.ACTIVE,
                    enabled=True,
                    max_concurrent_executions=2,
                    supported_profiles=["basic", "ml"],
                    connection_config={"endpoint": jupyter.endpoint},
                    **runtime_credential_fields(jupyter.token),
                )
            )
            await session.flush()
            await session.execute(
                update(ExecutionORM)
                .where(ExecutionORM.id == execution_id)
                .values(
                    status=ExecutionStatus.SUCCEEDED,
                    runtime_target_id=target_id,
                    workspace_path=workspace,
                    notebook_path=notebook_path,
                )
            )
            session.add(
                ExecutionArtifactORM(
                    id=artifact_id,
                    execution_id=execution_id,
                    artifact_type=ArtifactType.NOTEBOOK,
                    storage_type=ArtifactStorageType.PV,
                    status=ArtifactStatus.AVAILABLE,
                    name="execution.ipynb",
                    uri=f"pv://{notebook_path}",
                    relative_path=notebook_path,
                    media_type="application/x-ipynb+json",
                    size_bytes=len(original),
                    checksum_sha256=hashlib.sha256(original).hexdigest(),
                    identity_hash=uuid4().hex,
                )
            )

        async with (
            serve(create_app(container)) as executor_url,
            bff(executor_url) as bff_url,
        ):
            async with httpx.AsyncClient(
                base_url=bff_url, timeout=60
            ) as consumer:
                route = f"/api/v1/artifacts/{artifact_id}/content"
                await verify_file(consumer, route, original)
                report = await client.post(
                    f"/api/v1/executions/{execution_id}/artifacts",
                    json={
                        "idempotency_key": f"download-report-{profile}",
                        "type": "REPORT",
                        "source": {
                            "type": "INLINE",
                            "content": "# Final report\n\n"
                            + "Findings. " * 200,
                        },
                        "append_to_notebook": True,
                        "actor": {"type": "USER", "id": "download-test"},
                    },
                )
                assert report.status_code == 201, report.text
                grown = (jupyter.root / notebook_path).read_bytes()
                assert len(grown) > len(original)
                await verify_file(consumer, route, grown)
                assert (
                    len(nbformat.reads(grown.decode(), as_version=4).cells)
                    == 8
                )

                # Actual standard Contents save, as used for manual editing.
                manual = nbformat.v4.new_notebook(
                    cells=[nbformat.v4.new_markdown_cell("Manually edited")]
                )
                async with httpx.AsyncClient(
                    base_url=jupyter.endpoint,
                    headers={"Authorization": f"token {jupyter.token}"},
                    timeout=30,
                ) as direct:
                    saved = await direct.put(
                        f"/api/contents/{notebook_path}",
                        json={
                            "type": "notebook",
                            "format": "json",
                            "content": json.loads(nbformat.writes(manual)),
                        },
                    )
                    assert saved.status_code == 200, saved.text
                shrunk = (jupyter.root / notebook_path).read_bytes()
                assert len(shrunk) < len(original)
                await verify_file(consumer, route, shrunk)
                print(
                    json.dumps(
                        {
                            "profile": profile,
                            "original_bytes": len(original),
                            "report_bytes": len(grown),
                            "edited_bytes": len(shrunk),
                            "bff_executor_jupyter": "PASS",
                        }
                    )
                )
    finally:
        if kernel:
            await driver.delete_session(kernel)
        await driver.close()


async def verify_file(
    client: httpx.AsyncClient, route: str, original: bytes
) -> None:
    response = await client.get(route)
    assert response.status_code == 200, response.text[:100]
    assert response.content == original
    assert int(response.headers["Content-Length"]) == len(original)
    checksum = hashlib.sha256(original).hexdigest()
    assert response.headers["ETag"] == f'"{checksum}"'
    assert response.headers["X-Checksum-SHA256"] == checksum
    nbformat.validate(nbformat.reads(response.text, as_version=4))
    assembled = b""
    for index in range(5):
        start = len(original) * index // 5
        end = len(original) * (index + 1) // 5 - 1
        part = await client.get(
            route, headers={"Range": f"bytes={start}-{end}"}
        )
        assert part.status_code == 206
        assert (
            part.headers["Content-Range"]
            == f"bytes {start}-{end}/{len(original)}"
        )
        assert part.headers["ETag"] == f'"{checksum}"'
        assembled += part.content
    assert assembled == original
    invalid = await client.get(
        route, headers={"Range": f"bytes={len(original)}-"}
    )
    assert invalid.status_code == 416
    assert invalid.headers["Content-Range"] == f"bytes */{len(original)}"


async def test_large_binary_empty_file_and_atomic_replacement(
    jupyter: LiveJupyter,
    rest_client: tuple[httpx.AsyncClient, ApplicationContainer],
) -> None:
    client, container = rest_client
    submitted = await client.post("/api/v1/executions", json=_submit_payload())
    assert submitted.status_code == 202
    execution_id = UUID(submitted.json()["execution_id"])
    relative = (
        "users/download-test/projects/live/sessions/binary/"
        f"executions/{execution_id}/artifacts/logs/file.bin"
    )
    path = jupyter.root / relative
    path.parent.mkdir(parents=True)
    target_id, artifact_id = uuid4(), uuid4()
    async with container.session_factory() as session, session.begin():
        session.add(
            RuntimeTargetORM(
                id=target_id,
                name="binary-download",
                runtime_type=RuntimeType.JUPYTER,
                pool=RuntimePool.INTERACTIVE,
                status=RuntimeTargetStatus.ACTIVE,
                enabled=True,
                max_concurrent_executions=2,
                supported_profiles=["basic", "ml"],
                connection_config={"endpoint": jupyter.endpoint},
                **runtime_credential_fields(jupyter.token),
            )
        )
        await session.flush()
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution_id)
            .values(
                runtime_target_id=target_id,
            )
        )
        session.add(
            ExecutionArtifactORM(
                id=artifact_id,
                execution_id=execution_id,
                artifact_type=ArtifactType.OTHER,
                storage_type=ArtifactStorageType.PV,
                status=ArtifactStatus.AVAILABLE,
                name="file.bin",
                uri=f"pv://{relative}",
                relative_path=relative,
                media_type="application/octet-stream",
                size_bytes=1,
                checksum_sha256=hashlib.sha256(b"x").hexdigest(),
                identity_hash=uuid4().hex,
            )
        )
    route = f"/api/v1/artifacts/{artifact_id}/content"
    async with (
        serve(create_app(container)) as executor_url,
        bff(executor_url) as bff_url,
    ):
        async with httpx.AsyncClient(base_url=bff_url, timeout=60) as consumer:
            large = bytes(range(256)) * (32 * 1024) + b"end"
            path.write_bytes(large)
            async with consumer.stream("GET", route) as response:
                assert response.status_code == 200
                assert int(response.headers["Content-Length"]) == len(large)
                replacement = path.with_suffix(".replacement")
                replacement.write_bytes(b"new")
                os.replace(replacement, path)
                assert await response.aread() == large
            assert (await consumer.get(route)).content == b"new"
            path.write_bytes(b"NEW")  # Same byte count, new checksum.
            updated = await consumer.get(route)
            assert updated.content == b"NEW"
            assert (
                updated.headers["X-Checksum-SHA256"]
                == hashlib.sha256(b"NEW").hexdigest()
            )
            for header, expected, content_range in [
                ("bytes=-2", b"EW", "bytes 1-2/3"),
                ("bytes=1-", b"EW", "bytes 1-2/3"),
                ("bytes=0-99", b"NEW", "bytes 0-2/3"),
            ]:
                part = await consumer.get(route, headers={"Range": header})
                assert part.status_code == 206
                assert part.content == expected
                assert part.headers["Content-Range"] == content_range
            path.write_bytes(b"")
            empty = await consumer.get(route)
            assert empty.status_code == 200, empty.text
            assert empty.content == b""
            assert empty.headers["Content-Length"] == "0"
            invalid = await consumer.get(route, headers={"Range": "bytes=0-0"})
            assert invalid.status_code == 416
            assert invalid.headers["Content-Range"] == "bytes */0"
            path.unlink()
            missing = await consumer.get(route)
            assert missing.status_code != 200
            assert (
                missing.json()["error"]["code"]
                == "ARTIFACT_CONTENT_UNAVAILABLE"
            )
    async with container.session_factory() as session:
        record = await session.get(ExecutionArtifactORM, artifact_id)
        assert record is not None and record.size_bytes == 1
        assert record.checksum_sha256 == hashlib.sha256(b"x").hexdigest()
    print(
        json.dumps(
            {
                "binary_bytes": len(large),
                "atomic_replace": "PASS",
                "empty_range": "PASS",
            }
        )
    )
