"""Real REST/Jupyter smoke for live notebook discovery and atomic downloads.

Run through redis_compatibility_lifecycle_smoke with disposable dependencies.
Creates SINGLE/MULTI executions; never resets user DBs, Streams or files.
"""

import asyncio
import hashlib
import os
from time import monotonic
from typing import Any
from uuid import uuid4

import httpx
import nbformat
from execution_spec_payload import (
    SMOKE_HEADERS,
    execution_request,
    inline_spec,
)

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}


async def main() -> None:
    base_url = os.environ["EXECUTOR_REST_URL"]
    actor = {"type": "USER", "id": f"notebook-smoke-{uuid4().hex}"}
    profile = os.getenv("REDIS_COMPAT_RUNTIME_PROFILE", "default")
    checked_downloads = 0
    async with httpx.AsyncClient(
        base_url=base_url, timeout=30, headers=SMOKE_HEADERS
    ) as client:

        async def request(
            method: str, path: str, body: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            response = await client.request(method, path, json=body)
            response.raise_for_status()
            return response.json()

        async def state(eid: str) -> dict[str, Any]:
            return await request("GET", f"/executions/{eid}")

        async def wait(eid: str, statuses: set[str]) -> dict[str, Any]:
            deadline = monotonic() + 90
            while monotonic() < deadline:
                row = await state(eid)
                if row["state"]["status"] in statuses:
                    return row
                await asyncio.sleep(0.15)
            raise TimeoutError(row)

        async def notebook(eid: str) -> dict[str, Any]:
            response = await request("GET", f"/executions/{eid}/artifacts")
            rows = [r for r in response["items"] if r["type"] == "NOTEBOOK"]
            assert len(rows) == 1, response
            assert rows[0]["status"] == "AVAILABLE", rows
            return rows[0]

        async def download(artifact_id: str) -> dict[str, Any]:
            nonlocal checked_downloads
            response = await client.get(f"/artifacts/{artifact_id}/content")
            response.raise_for_status()
            assert response.status_code == 200
            body = response.content
            assert len(body) == int(response.headers["content-length"])
            digest = hashlib.sha256(body).hexdigest()
            assert response.headers["x-checksum-sha256"] == digest
            assert response.headers["etag"] == f'"{digest}"'
            document = nbformat.reads(body.decode(), as_version=4)
            nbformat.validate(document)
            checked_downloads += 1
            return dict(document)

        async def command(eid: str, action: str) -> dict[str, Any]:
            current = await state(eid)
            return await request(
                "POST",
                f"/executions/{eid}/{action}",
                {
                    "idempotency_key": uuid4().hex,
                    "actor": actor,
                    **(
                        {"expected_version": current["state"]["version"]}
                        if action == "finalize"
                        else {}
                    ),
                },
            )

        for mode, outcome in (
            ("SINGLE", "success"),
            ("SINGLE", "retry"),
            ("SINGLE", "cancel"),
            ("MULTI", "success"),
        ):
            slow = (
                "import time\nprint('before')\ntime.sleep(4)\nprint('after')"
            )
            code = "print('saved output ' * 8000)"
            if outcome == "retry":
                code = (
                    "attempt = globals().get('attempt', 0) + 1\n"
                    "print('attempt', attempt)\n"
                    "if attempt == 1: raise ValueError('expected failure')"
                )
            if outcome == "cancel":
                slow = "import time\ntime.sleep(60)"
            receipt = await request(
                "POST",
                "/executions",
                execution_request(
                    idempotency_key=uuid4().hex,
                    operation_mode=mode,
                    operation_wait_timeout_seconds=600
                    if mode == "MULTI"
                    else None,
                    trigger_type="INTERACTIVE",
                    actor=actor,
                    runtime_profile=profile,
                    spec=inline_spec([{"code": slow}, {"code": code}]),
                ),
            )
            eid = receipt["execution_id"]
            await wait(eid, {"RUNNING"})
            # RUNNING starts before preparation; wait for the first saved file.
            for _ in range(100):
                rows = await request("GET", f"/executions/{eid}/artifacts")
                if any(r["type"] == "NOTEBOOK" for r in rows["items"]):
                    break
                await asyncio.sleep(0.1)
            first = await notebook(eid)
            artifact_id = first["artifact_id"]
            initial = await download(artifact_id)
            assert not initial["cells"][0]["outputs"], initial
            if outcome == "cancel":
                await command(eid, "cancel")
                cancelled = await wait(eid, TERMINAL)
                assert cancelled["state"]["status"] == "CANCELLED"
            else:
                deadline = monotonic() + 90
                while monotonic() < deadline:
                    # Parallel readers span notebook replacement during Steps.
                    await asyncio.gather(
                        *(download(artifact_id) for _ in range(4))
                    )
                    current = await state(eid)
                    if current["state"]["status"] in TERMINAL | {
                        "WAITING_FOR_OPERATION"
                    }:
                        break
                    await asyncio.sleep(0.15)
                else:
                    raise TimeoutError(current)
                saved = await download(artifact_id)
                assert saved["cells"][0]["outputs"]
                if outcome == "retry":
                    assert current["state"]["status"] == "FAILED"
                    assert any(
                        output["output_type"] == "error"
                        for output in saved["cells"][1]["outputs"]
                    )
                    await command(eid, "retry")
                    finished = await wait(eid, TERMINAL)
                    assert finished["state"]["status"] == "SUCCEEDED"
                elif mode == "MULTI":
                    assert current["state"]["status"] == (
                        "WAITING_FOR_OPERATION"
                    )
                    await command(eid, "finalize")
                    finished = await wait(eid, TERMINAL)
                    assert finished["state"]["status"] == "SUCCEEDED"
                else:
                    assert current["state"]["status"] == "SUCCEEDED"
            assert (await notebook(eid))["artifact_id"] == artifact_id
            await download(artifact_id)
            detail = await request("GET", f"/artifacts/{artifact_id}")
            assert detail["produced_by"]["execution_step_id"] is None
            print(f"PASS {mode}/{outcome}: {eid} artifact={artifact_id}")
        print(f"PASS validated downloads: {checked_downloads}")


if __name__ == "__main__":
    asyncio.run(main())
