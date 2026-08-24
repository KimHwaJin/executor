"""Verify SINGLE execution state, persistence, events, artifacts, and session cleanup.

The script submits one execution through REST and one through MCP. It expects an already running
Executor stack with PostgreSQL, Redis, and at least one schedulable INTERACTIVE Runtime Target.
"""

import asyncio
import os
from dataclasses import dataclass
from time import monotonic
from typing import Any, Literal
from uuid import UUID, uuid4

import httpx
from execution_spec_payload import execution_request, inline_spec
from mcp import Client
from redis.asyncio import Redis
from sqlalchemy import select

from executor_service.config import get_settings
from executor_service.domain.enums import OutboxDestination
from executor_service.events import (
    EXECUTION_EVENT_SCHEMA_VERSION,
    ExecutionStreamEnvelope,
)
from executor_service.infrastructure.db.models import (
    ExecutionArtifactORM,
    ExecutionAttemptORM,
    ExecutionORM,
    ExecutionStepAttemptORM,
    ExecutionStepORM,
    OutboxEventORM,
)
from executor_service.infrastructure.db.session import (
    create_engine,
    create_session_factory,
)

Transport = Literal["REST", "MCP"]
TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED"}


@dataclass(frozen=True)
class DatabaseSnapshot:
    execution_status: str
    step_statuses: tuple[str, ...]
    attempt_statuses: tuple[str, ...]
    step_attempt_statuses: tuple[str, ...]
    artifact_names: tuple[str, ...]
    outbox_event_ids: frozenset[str]
    outbox_event_types: tuple[str, ...]
    outbox_statuses: tuple[str, ...]
    outbox_schema_versions: tuple[str | None, ...]


@dataclass(frozen=True)
class CaseResult:
    transport: Transport
    execution_id: str
    target_id: str
    observed_states: tuple[str, ...]
    event_types: tuple[str, ...]
    artifact_names: tuple[str, ...]
    notebook_path: str
    runtime_session_id: str
    redis_delivery_count: int


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


async def _mcp_result(
    client: Client, tool: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    if result.is_error or result.structured_content is None:
        raise RuntimeError(f"{tool} failed: {result.content}")
    return result.structured_content


async def _rest_json(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = await client.request(method, path.lstrip("/"), json=json)
    response.raise_for_status()
    return response.json()


def _submission_payload(
    unique: str, transport: Transport, runtime_profile: str
) -> dict[str, Any]:
    label = transport.lower()
    user_id = f"single-observability-{label}-user"
    return execution_request(
        idempotency_key=f"single-observability-{label}-submit-{unique}",
        operation_mode="SINGLE",
        trigger_type="INTERACTIVE",
        runtime_profile=runtime_profile,
        spec=inline_spec(
            [
                {
                    "skill_name": "eda",
                    "tool_name": "observe_running_state",
                    "code": (
                        "import time\n"
                        f"transport = {transport!r}\n"
                        "time.sleep(1.0)\n"
                        "print(f'completed through {transport}')"
                    ),
                },
                {
                    "skill_name": "report",
                    "tool_name": "write_observability_artifact",
                    "code": (
                        "from pathlib import Path\n"
                        f"artifact = Path('artifacts/other/{label}-observability.txt')\n"
                        "artifact.write_text(transport, encoding='utf-8')\n"
                        "print(artifact)"
                    ),
                },
            ],
        ),
        context={
            "user_id": user_id,
            "project_id": "single-observability-project",
            "session_id": f"single-observability-{label}-session-{unique}",
            "task_id": f"single-observability-{label}-task-{unique}",
        },
        actor={"type": "USER", "id": user_id},
    )


async def _execution_get(
    transport: Transport,
    execution_id: str,
    *,
    mcp: Client,
    rest: httpx.AsyncClient,
) -> dict[str, Any]:
    if transport == "MCP":
        return await _mcp_result(
            mcp, "execution_get", {"execution_id": execution_id}
        )
    return await _rest_json(rest, "GET", f"/executions/{execution_id}")


async def _history_page(
    transport: Transport,
    execution_id: str,
    kind: Literal["attempts", "events", "artifacts"],
    *,
    mcp: Client,
    rest: httpx.AsyncClient,
) -> list[dict[str, Any]]:
    if transport == "MCP":
        tool = {
            "attempts": "execution_attempt_list",
            "events": "execution_event_list",
            "artifacts": "execution_artifact_list",
        }[kind]
        page = await _mcp_result(
            mcp,
            tool,
            {
                "execution_id": execution_id,
                "limit": 500 if kind != "attempts" else 100,
            },
        )
    else:
        page = await _rest_json(
            rest,
            "GET",
            f"/executions/{execution_id}/{kind}?limit={500 if kind != 'attempts' else 100}",
        )
    if page["has_more"] or page["next_cursor"] is not None:
        raise RuntimeError(
            f"Unexpected pagination for observability {kind}: {page}"
        )
    return page["items"]


async def _execution_steps(
    transport: Transport,
    execution_id: str,
    *,
    mcp: Client,
    rest: httpx.AsyncClient,
) -> list[dict[str, Any]]:
    if transport == "MCP":
        page = await _mcp_result(
            mcp,
            "execution_step_list",
            {"execution_id": execution_id, "limit": 100},
        )
    else:
        page = await _rest_json(
            rest, "GET", f"/executions/{execution_id}/steps?limit=100"
        )
    return page["items"]


async def _attempt_detail(
    transport: Transport,
    execution_id: str,
    attempt_id: str,
    *,
    mcp: Client,
    rest: httpx.AsyncClient,
) -> dict[str, Any]:
    if transport == "MCP":
        return await _mcp_result(
            mcp,
            "execution_attempt_get",
            {"execution_id": execution_id, "attempt_id": attempt_id},
        )
    return await _rest_json(
        rest, "GET", f"/executions/{execution_id}/attempts/{attempt_id}"
    )


async def _attempt_steps(
    transport: Transport,
    execution_id: str,
    attempt_id: str,
    *,
    mcp: Client,
    rest: httpx.AsyncClient,
) -> list[dict[str, Any]]:
    if transport == "MCP":
        page = await _mcp_result(
            mcp,
            "execution_attempt_step_list",
            {
                "execution_id": execution_id,
                "attempt_id": attempt_id,
                "limit": 100,
            },
        )
    else:
        page = await _rest_json(
            rest,
            "GET",
            f"/executions/{execution_id}/attempts/{attempt_id}/steps?limit=100",
        )
    return page["items"]


async def _wait_for_terminal(
    transport: Transport,
    execution_id: str,
    *,
    mcp: Client,
    rest: httpx.AsyncClient,
    timeout_seconds: float,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    deadline = monotonic() + timeout_seconds
    observed = ["QUEUED"]
    while monotonic() < deadline:
        execution = await _execution_get(
            transport, execution_id, mcp=mcp, rest=rest
        )
        status = execution["state"]["status"]
        if status != observed[-1]:
            observed.append(status)
        if status in TERMINAL_STATUSES:
            return execution, tuple(observed)
        await asyncio.sleep(0.1)
    raise RuntimeError(
        f"Execution {execution_id} did not finish within {timeout_seconds}s."
    )


async def _database_snapshot(
    session_factory: Any, execution_id: UUID
) -> DatabaseSnapshot:
    async with session_factory() as session:
        execution = await session.get(ExecutionORM, execution_id)
        if execution is None:
            raise RuntimeError(
                f"Execution {execution_id} is absent from PostgreSQL."
            )
        steps = list(
            await session.scalars(
                select(ExecutionStepORM)
                .where(ExecutionStepORM.execution_id == execution_id)
                .order_by(ExecutionStepORM.sequence)
            )
        )
        attempts = list(
            await session.scalars(
                select(ExecutionAttemptORM)
                .where(ExecutionAttemptORM.execution_id == execution_id)
                .order_by(ExecutionAttemptORM.attempt_number)
            )
        )
        step_attempts = list(
            await session.scalars(
                select(ExecutionStepAttemptORM)
                .where(ExecutionStepAttemptORM.execution_id == execution_id)
                .order_by(ExecutionStepAttemptORM.sequence)
            )
        )
        artifacts = list(
            await session.scalars(
                select(ExecutionArtifactORM)
                .where(ExecutionArtifactORM.execution_id == execution_id)
                .order_by(
                    ExecutionArtifactORM.created_at, ExecutionArtifactORM.id
                )
            )
        )
        events = list(
            await session.scalars(
                select(OutboxEventORM)
                .where(
                    OutboxEventORM.aggregate_type == "Execution",
                    OutboxEventORM.aggregate_id == execution_id,
                    OutboxEventORM.destination == OutboxDestination.EVENTS,
                )
                .order_by(OutboxEventORM.created_at, OutboxEventORM.id)
            )
        )
    return DatabaseSnapshot(
        execution_status=_enum_value(execution.status),
        step_statuses=tuple(_enum_value(row.status) for row in steps),
        attempt_statuses=tuple(_enum_value(row.status) for row in attempts),
        step_attempt_statuses=tuple(
            _enum_value(row.status) for row in step_attempts
        ),
        artifact_names=tuple(row.name for row in artifacts),
        outbox_event_ids=frozenset(str(row.id) for row in events),
        outbox_event_types=tuple(row.event_type for row in events),
        outbox_statuses=tuple(_enum_value(row.status) for row in events),
        outbox_schema_versions=tuple(
            row.payload.get("schema_version") for row in events
        ),
    )


async def _wait_for_published_snapshot(
    session_factory: Any, execution_id: UUID, timeout_seconds: float
) -> DatabaseSnapshot:
    deadline = monotonic() + timeout_seconds
    snapshot = await _database_snapshot(session_factory, execution_id)
    while monotonic() < deadline:
        if snapshot.outbox_statuses and set(snapshot.outbox_statuses) == {
            "PUBLISHED"
        }:
            return snapshot
        await asyncio.sleep(0.1)
        snapshot = await _database_snapshot(session_factory, execution_id)
    raise RuntimeError(
        f"Outbox events were not all PUBLISHED: {snapshot.outbox_statuses}"
    )


async def _redis_events(
    redis: Redis,
    stream: str,
    execution_id: str,
    *,
    scan_limit: int,
) -> list[dict[str, str]]:
    rows = await redis.xrevrange(stream, count=scan_limit)
    return [
        fields
        for _, fields in rows
        if fields.get("aggregate_id") == execution_id
    ]


def _assert_database(snapshot: DatabaseSnapshot) -> None:
    if snapshot.execution_status != "SUCCEEDED":
        raise RuntimeError(
            f"PostgreSQL Execution status is {snapshot.execution_status}."
        )
    if snapshot.step_statuses != ("SUCCEEDED", "SUCCEEDED"):
        raise RuntimeError(
            f"Unexpected PostgreSQL Step states: {snapshot.step_statuses}"
        )
    if snapshot.attempt_statuses != ("SUCCEEDED",):
        raise RuntimeError(
            f"Expected exactly one successful Attempt: {snapshot.attempt_statuses}"
        )
    if snapshot.step_attempt_statuses != ("SUCCEEDED", "SUCCEEDED"):
        raise RuntimeError(
            f"Unexpected PostgreSQL Step Attempt states: {snapshot.step_attempt_statuses}"
        )
    required_events = {
        "execution.submitted",
        "execution.started",
        "execution.succeeded",
    }
    if not required_events.issubset(snapshot.outbox_event_types):
        raise RuntimeError(
            f"Required Outbox events are missing: {snapshot.outbox_event_types}"
        )
    if set(snapshot.outbox_schema_versions) != {
        EXECUTION_EVENT_SCHEMA_VERSION
    }:
        raise RuntimeError(
            f"Unexpected Outbox schema versions: {snapshot.outbox_schema_versions}"
        )


async def _run_case(
    transport: Transport,
    *,
    unique: str,
    runtime_profile: str,
    timeout_seconds: float,
    scan_limit: int,
    mcp: Client,
    rest: httpx.AsyncClient,
    session_factory: Any,
    redis: Redis,
    stream: str,
) -> CaseResult:
    payload = _submission_payload(unique, transport, runtime_profile)
    if transport == "MCP":
        submitted = await _mcp_result(
            mcp, "execution_submit", {"request": payload}
        )
    else:
        submitted = await _rest_json(rest, "POST", "/executions", json=payload)
    if submitted["state"]["status"] != "QUEUED":
        raise RuntimeError(f"Submit did not return QUEUED: {submitted}")

    execution_id = str(submitted["execution_id"])
    terminal, observed_states = await _wait_for_terminal(
        transport,
        execution_id,
        mcp=mcp,
        rest=rest,
        timeout_seconds=timeout_seconds,
    )
    if terminal["state"]["status"] != "SUCCEEDED":
        raise RuntimeError(f"Execution did not succeed: {terminal}")
    if "RUNNING" not in observed_states:
        raise RuntimeError(
            f"Execution RUNNING state was not observed: {observed_states}"
        )
    current_steps = await _execution_steps(
        transport, execution_id, mcp=mcp, rest=rest
    )
    if [step["result"]["status"] for step in current_steps] != [
        "SUCCEEDED",
        "SUCCEEDED",
    ]:
        raise RuntimeError(
            f"Current Step results are not successful: {current_steps}"
        )

    attempts = await _history_page(
        transport, execution_id, "attempts", mcp=mcp, rest=rest
    )
    if len(attempts) != 1 or attempts[0]["state"]["status"] != "SUCCEEDED":
        raise RuntimeError(
            f"Expected exactly one successful Attempt: {attempts}"
        )
    attempt_id = str(attempts[0]["attempt_id"])
    attempt = await _attempt_detail(
        transport, execution_id, attempt_id, mcp=mcp, rest=rest
    )
    attempt_steps = await _attempt_steps(
        transport, execution_id, attempt_id, mcp=mcp, rest=rest
    )
    if [step["result"]["status"] for step in attempt_steps] != [
        "SUCCEEDED",
        "SUCCEEDED",
    ]:
        raise RuntimeError(
            f"Attempt Step history is incomplete: {attempt_steps}"
        )
    runtime_session_id = attempt["runtime"]["session_id"]
    if not runtime_session_id or terminal["runtime"]["session_id"] is not None:
        raise RuntimeError(
            "Runtime session history or terminal cleanup state is invalid."
        )

    artifacts = await _history_page(
        transport, execution_id, "artifacts", mcp=mcp, rest=rest
    )
    artifact_names = tuple(sorted(item["name"] for item in artifacts))
    required_artifacts = {
        f"{transport.lower()}-observability.txt",
        "execution.ipynb",
    }
    if not required_artifacts.issubset(artifact_names):
        raise RuntimeError(f"Required Artifacts are missing: {artifact_names}")

    snapshot = await _wait_for_published_snapshot(
        session_factory, UUID(execution_id), timeout_seconds
    )
    _assert_database(snapshot)
    events = await _history_page(
        transport, execution_id, "events", mcp=mcp, rest=rest
    )
    if set(snapshot.artifact_names) != set(artifact_names):
        raise RuntimeError(
            "Artifact API and PostgreSQL differ: "
            f"api={artifact_names}, db={snapshot.artifact_names}"
        )
    event_ids = {str(item["event_id"]) for item in events}
    if event_ids != snapshot.outbox_event_ids:
        raise RuntimeError("Event API and PostgreSQL Outbox event IDs differ.")
    if any(item["delivery"]["status"] != "PUBLISHED" for item in events):
        raise RuntimeError(
            f"Event API still reports an unpublished event: {events}"
        )

    redis_rows = await _redis_events(
        redis, stream, execution_id, scan_limit=scan_limit
    )
    redis_event_ids = {row["event_id"] for row in redis_rows}
    if redis_event_ids != snapshot.outbox_event_ids:
        missing = snapshot.outbox_event_ids - redis_event_ids
        unexpected = redis_event_ids - snapshot.outbox_event_ids
        raise RuntimeError(
            f"Redis Stream and Outbox differ: missing={missing}, unexpected={unexpected}"
        )
    envelopes = [
        ExecutionStreamEnvelope.from_redis_fields(row) for row in redis_rows
    ]
    if any(
        envelope.schema_version != EXECUTION_EVENT_SCHEMA_VERSION
        for envelope in envelopes
    ):
        raise RuntimeError("Redis Stream contains a non-v2 Execution event.")

    notebook_path = terminal["workspace"]["notebook_path"]
    if not notebook_path:
        raise RuntimeError("Execution did not return workspace.notebook_path.")
    notebook = await _rest_json(
        rest,
        "GET",
        f"/executions/{execution_id}/notebook?response_format=detailed&limit=0",
    )
    if notebook["page"]["total_count"] != 2:
        raise RuntimeError(f"Runtime-owned notebook is incomplete: {notebook}")

    return CaseResult(
        transport=transport,
        execution_id=execution_id,
        target_id=str(terminal["runtime"]["target_id"]),
        observed_states=observed_states,
        event_types=snapshot.outbox_event_types,
        artifact_names=artifact_names,
        notebook_path=notebook_path,
        runtime_session_id=runtime_session_id,
        redis_delivery_count=len(redis_rows),
    )


async def _probe_target(
    mcp: Client, target_id: str, unique: str
) -> dict[str, Any]:
    return await _mcp_result(
        mcp,
        "runtime_target_probe",
        {
            "request": {
                "target_id": target_id,
                "actor": {
                    "type": "USER",
                    "id": f"single-observability-{unique}",
                },
            }
        },
    )


async def main() -> None:
    settings = get_settings()
    mcp_url = os.getenv("EXECUTOR_MCP_URL", "http://127.0.0.1:8000/mcp")
    rest_url = os.getenv("EXECUTOR_REST_URL", "http://127.0.0.1:8000/api/v1")
    runtime_profile = os.getenv("OBSERVABILITY_RUNTIME_PROFILE", "basic")
    timeout_seconds = float(os.getenv("OBSERVABILITY_TIMEOUT_SECONDS", "120"))
    scan_limit = int(os.getenv("OBSERVABILITY_STREAM_SCAN_LIMIT", "2000"))
    unique = uuid4().hex

    engine = create_engine(settings.database_dsn)
    session_factory = create_session_factory(engine)
    redis = Redis.from_url(settings.redis_dsn, decode_responses=True)
    try:
        async with (
            Client(mcp_url) as mcp,
            httpx.AsyncClient(
                base_url=f"{rest_url.rstrip('/')}/", timeout=30
            ) as rest,
        ):
            results = []
            for transport in ("REST", "MCP"):
                results.append(
                    await _run_case(
                        transport,
                        unique=unique,
                        runtime_profile=runtime_profile,
                        timeout_seconds=timeout_seconds,
                        scan_limit=scan_limit,
                        mcp=mcp,
                        rest=rest,
                        session_factory=session_factory,
                        redis=redis,
                        stream=settings.redis_event_stream,
                    )
                )

            target_ids = {result.target_id for result in results}
            probes = [
                await _probe_target(mcp, target_id, unique)
                for target_id in target_ids
            ]
            leaked_targets = [
                probe
                for probe in probes
                if probe["capacity"]["active_execution_count"] != 0
                or probe["capacity"]["active_session_count"] != 0
            ]
            if leaked_targets:
                raise RuntimeError(
                    f"Execution or Runtime session leaked: {leaked_targets}"
                )

        for result in results:
            print(f"[{result.transport}]")
            print("execution_id:", result.execution_id)
            print("states:", " -> ".join(result.observed_states))
            print("runtime_target_id:", result.target_id)
            print("historical_runtime_session_id:", result.runtime_session_id)
            print("attempt_count:", 1)
            print("outbox_and_redis_events:", list(result.event_types))
            print("redis_delivery_count:", result.redis_delivery_count)
            print("artifacts:", list(result.artifact_names))
            print("notebook:", result.notebook_path)
        print("runtime_session_leaks:", 0)
    finally:
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
