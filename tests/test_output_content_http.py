import hashlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx

from executor_service.application.output_contents import (
    ExecutionOutputContentDescriptor,
)
from executor_service.config import Settings
from executor_service.container import ApplicationContainer
from executor_service.domain.enums import RuntimeType
from executor_service.domain.runtime import RuntimeOutputJournalIdentity
from executor_service.interfaces.http.app import create_app

EXECUTION_ID = UUID("11111111-1111-4111-8111-111111111111")
OUTPUT_ID = UUID("77777777-7777-4777-8777-777777777777")
REPRESENTATION_ID = UUID("88888888-8888-4888-8888-888888888888")


class _OutputContents:
    body = b"0123456789"

    async def describe(
        self,
        execution_id: UUID,
        output_id: UUID,
        representation_id: UUID,
    ) -> ExecutionOutputContentDescriptor:
        return ExecutionOutputContentDescriptor(
            execution_id=execution_id,
            output_id=output_id,
            representation_id=representation_id,
            runtime_type=RuntimeType.JUPYTER,
            preferred_target_id=UUID("55555555-5555-4555-8555-555555555555"),
            identity=RuntimeOutputJournalIdentity(
                workspace_path="users/u/executions/e",
                execution_id=execution_id,
                operation_id=UUID("22222222-2222-4222-8222-222222222222"),
                step_id=UUID("33333333-3333-4333-8333-333333333333"),
                sequence=0,
                execution_attempt_id=UUID(
                    "44444444-4444-4444-8444-444444444444"
                ),
                fencing_token=1,
                runtime_target_id=UUID("55555555-5555-4555-8555-555555555555"),
                runtime_session_id="kernel-1",
            ),
            journal_id=UUID("66666666-6666-4666-8666-666666666666"),
            media_type="text/plain",
            size_bytes=len(self.body),
            checksum_sha256=hashlib.sha256(self.body).hexdigest(),
            complete=True,
        )

    async def stream(
        self,
        descriptor: ExecutionOutputContentDescriptor,
        start: int,
        end_exclusive: int,
    ) -> AsyncIterator[bytes]:
        del descriptor
        yield self.body[start:end_exclusive]

    async def read_inline_text(
        self,
        descriptor: ExecutionOutputContentDescriptor,
        *,
        max_bytes: int,
    ) -> str | None:
        del descriptor, max_bytes
        return self.body.decode()


async def test_rest_output_content_supports_full_and_range_reads(
    tmp_path: Path,
) -> None:
    container = ApplicationContainer(
        Settings(
            database_url="sqlite+aiosqlite:///:memory:",
            redis_url="redis://localhost:6399/15",
            runtime_enabled=False,
            input_host_root=tmp_path,
        )
    )
    cast(Any, container).output_contents = _OutputContents()
    app = create_app(container)
    path = (
        f"/api/v1/executions/{EXECUTION_ID}/outputs/{OUTPUT_ID}/"
        f"representations/{REPRESENTATION_ID}/content"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        full = await client.get(path)
        partial = await client.get(path, headers={"Range": "bytes=2-5"})
        suffix = await client.get(path, headers={"Range": "bytes=-3"})
        invalid = await client.get(path, headers={"Range": "bytes=20-"})

    assert full.status_code == 200
    assert full.content == b"0123456789"
    assert full.headers["accept-ranges"] == "bytes"
    assert partial.status_code == 206
    assert partial.content == b"2345"
    assert partial.headers["content-range"] == "bytes 2-5/10"
    assert suffix.content == b"789"
    assert invalid.status_code == 416
    assert invalid.headers["content-range"] == "bytes */10"
    await container.redis.aclose()
    await container.engine.dispose()
