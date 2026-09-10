"""Verify real kernel loss through Executor, PostgreSQL, Outbox and Redis.

Run inside redis_compatibility_lifecycle_smoke.main(smoke_scripts=(...)) with
a dedicated local Jupyter. Only this script's newly created kernels are killed
or restarted. No OOM, host inspection, existing-kernel modification or reset.
"""

import asyncio
import json
import os
from time import monotonic
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from execution_spec_payload import execution_request, inline_spec
from redis.asyncio import Redis
from sqlalchemy import select

from executor_service.infrastructure.db.models import (
    ExecutionOperationORM,
    ExecutionORM,
    ExecutionStepORM,
)
from executor_service.infrastructure.db.session import (
    create_engine,
    create_session_factory,
)
from executor_service.settings import get_settings


async def get(client: httpx.AsyncClient, path: str) -> dict[str, Any]:
    response = await client.get(path)
    response.raise_for_status()
    return response.json()


async def wait_state(
    rest: httpx.AsyncClient, execution_id: str, states: set[str]
) -> dict[str, Any]:
    async with asyncio.timeout(60):
        while True:
            execution = await get(rest, f"executions/{execution_id}")
            if execution["state"]["status"] in states:
                return execution
            await asyncio.sleep(0.2)


async def main() -> None:
    settings = get_settings()
    rest_url = os.environ["EXECUTOR_REST_URL"].rstrip("/") + "/"
    endpoint = os.environ["REDIS_COMPAT_JUPYTER_ENDPOINT"].rstrip("/") + "/"
    for url in (rest_url, endpoint):
        if urlsplit(url).hostname not in {"localhost", "127.0.0.1"}:
            raise ValueError(
                "Execution-loss smoke is restricted to localhost."
            )
    if "executor_redis_compat_" not in settings.database_dsn:
        raise ValueError("Use the isolated compatibility harness database.")
    engine = create_engine(settings.database_dsn)
    sessions = create_session_factory(engine)
    redis = Redis.from_url(settings.redis_dsn, decode_responses=True)
    profile = os.environ.get("REDIS_COMPAT_RUNTIME_PROFILE", "default")
    actor = {"type": "USER", "id": "continuity-smoke"}
    try:
        async with (
            httpx.AsyncClient(base_url=rest_url, timeout=30) as rest,
            httpx.AsyncClient(
                base_url=endpoint,
                headers={
                    "Authorization": "token "
                    + os.environ["REDIS_COMPAT_JUPYTER_TOKEN"]
                },
                timeout=30,
            ) as jupyter,
        ):
            missing = await get(
                jupyter, f"executor/kernels/{uuid4()}/execution-state"
            )
            assert missing["alive"] is False and missing["instance_id"] is None
            for mode, action in (
                ("SINGLE", "silent"),
                ("SINGLE", "exit"),
                ("MULTI", "exit"),
                ("SINGLE", "restart"),
                ("MULTI", "restart"),
            ):
                unique = uuid4().hex
                middle = (
                    "import time\nprint('partial-output', flush=True)\n"
                    + (
                        "time.sleep(1)\nimport os, signal\n"
                        "os.kill(os.getpid(), signal.SIGKILL)"
                        if action == "exit"
                        else "time.sleep(300)"
                        if action == "restart"
                        else "time.sleep(12)\nprint('silent-complete')"
                    )
                )
                request = execution_request(
                    idempotency_key=unique,
                    operation_mode=mode,
                    trigger_type="INTERACTIVE",
                    actor=actor,
                    runtime_profile=profile,
                    context={"user_id": actor["id"], "task_id": unique},
                    operation_wait_timeout_seconds=60
                    if mode == "MULTI"
                    else None,
                    # Deliberately no Step/Operation timeout: detection must not
                    # depend on the user's execution budget expiring.
                    spec=inline_spec(
                        [
                            {"code": "print('completed-first-step')"},
                            {"code": middle},
                            {"code": "print('last-step')"},
                        ]
                    ),
                )
                response = await rest.post("executions", json=request)
                response.raise_for_status()
                receipt = response.json()
                execution_id = receipt["execution_id"]
                second_id = receipt["operation"]["steps"][1]["step_id"]
                started = monotonic()
                if action == "restart":
                    async with asyncio.timeout(30):
                        while True:
                            step = await get(
                                rest,
                                f"executions/{execution_id}/steps/{second_id}",
                            )
                            execution = await get(
                                rest, f"executions/{execution_id}"
                            )
                            kernel_id = execution["runtime"]["session_id"]
                            if (
                                kernel_id
                                and step["result"]["status"] == "RUNNING"
                            ):
                                kernel = await get(
                                    jupyter, f"api/kernels/{kernel_id}"
                                )
                                if kernel["execution_state"] == "busy":
                                    break
                            await asyncio.sleep(0.1)
                    # Only the kernel created for this exact Execution is touched.
                    await asyncio.sleep(0.5)
                    restart = await jupyter.post(
                        f"api/kernels/{kernel_id}/restart"
                    )
                    restart.raise_for_status()
                    assert restart.json()["id"] == kernel_id
                execution = await wait_state(
                    rest, execution_id, {"FAILED", "SUCCEEDED", "CANCELLED"}
                )
                expected = "SUCCEEDED" if action == "silent" else "FAILED"
                assert execution["state"]["status"] == expected, execution
                terminal_seconds = monotonic() - started
                if expected == "FAILED":
                    assert (
                        execution["failure"]["type"] == "RUNTIME_SESSION_LOST"
                    ), execution
                    assert execution["retry"]["strategy"] != "FROM_FAILED_STEP"
                async with sessions() as session:
                    row = await session.get(ExecutionORM, UUID(execution_id))
                    assert row is not None and row.status.value == expected
                    operation = await session.get(
                        ExecutionOperationORM,
                        UUID(receipt["operation"]["operation_id"]),
                    )
                    assert (
                        operation is not None
                        and operation.status.value == expected
                    )
                    steps = list(
                        await session.scalars(
                            select(ExecutionStepORM)
                            .where(ExecutionStepORM.execution_id == row.id)
                            .order_by(ExecutionStepORM.sequence)
                        )
                    )
                    assert steps[0].status.value == "SUCCEEDED"
                    if expected == "FAILED":
                        assert steps[1].status.value == "FAILED"
                        assert steps[2].started_at is None
                        assert steps[1].result_complete is False
                        assert steps[1].result_manifest_path
                        manifest_path = (
                            settings.shared_storage_root
                            / steps[1].result_manifest_path
                        )
                        assert manifest_path.is_file()
                        manifest = json.loads(manifest_path.read_text())
                        assert manifest["complete"] is False
                        texts = [
                            (
                                settings.shared_storage_root
                                / representation["relative_path"]
                            ).read_text()
                            for output in manifest["outputs"]
                            for representation in output["representations"]
                            if representation["encoding"] == "UTF8"
                        ]
                        assert "partial-output" in "".join(texts)
                async with asyncio.timeout(15):
                    while True:
                        events = [
                            fields
                            for _, fields in await redis.xrange(
                                settings.redis_event_stream
                            )
                            if fields.get("execution_id") == execution_id
                        ]
                        terminals = [
                            e
                            for e in events
                            if e["event_type"] == "execution.completed"
                        ]
                        if terminals:
                            break
                        await asyncio.sleep(0.1)
                assert len(terminals) == 1
                payload = json.loads(terminals[0]["payload"])
                assert payload["status"] == expected, payload
                assert any(
                    e["event_type"] == "execution.operation_completed"
                    for e in events
                )
                # Manual restart can temporarily reject concurrent cleanup.
                # Confirm the existing durable cleanup retry actually releases
                # that session before the next case needs the one-slot target.
                async with asyncio.timeout(90):
                    while execution["runtime"]["session_id"] is not None:
                        await asyncio.sleep(0.2)
                        execution = await get(
                            rest, f"executions/{execution_id}"
                        )
                assert (
                    execution["recovery"]["runtime_session_cleanup_status"]
                    == "SUCCEEDED"
                )
                print(
                    f"PASS {mode}/{action} {execution_id} terminal_after={terminal_seconds:.2f}s DB={expected} terminal={expected} cleanup=SUCCEEDED",
                    flush=True,
                )
    finally:
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
