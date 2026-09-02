"""Exercise SINGLE failure/retry and cancellation across every durable Executor boundary.

The failure/retry case enters through MCP. The cancellation case enters through REST. Both cases
cross-check the public history API, PostgreSQL, Transactional Outbox, Redis Stream, shared-PV
Artifacts, and Jupyter Runtime session lifecycle against an already running local Compose stack.
"""

import asyncio
import hashlib
import os
from dataclasses import dataclass
from time import monotonic
from typing import Any, Literal
from uuid import UUID, uuid4

import httpx
from execution_spec_payload import execution_request, inline_spec
from mcp import Client
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.config import get_settings
from executor_service.events import (
    EXECUTION_EVENT_SCHEMA_VERSION,
    ExecutionStreamEnvelope,
)
from executor_service.infrastructure.db.models import (
    ExecutionArtifactORM,
    ExecutionAttemptORM,
    ExecutionEventORM,
    ExecutionOperationORM,
    ExecutionORM,
    ExecutionStepAttemptORM,
    ExecutionStepORM,
    OutboxEventORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.db.session import (
    create_engine,
    create_session_factory,
)
from executor_service.infrastructure.runtime_drivers import (
    ConfiguredRuntimeDriverFactory,
)
from executor_service.infrastructure.runtime_registry import (
    RuntimeTargetRegistry,
)

Transport = Literal["MCP", "REST"]
TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED"}


@dataclass(frozen=True)
class DatabaseSnapshot:
    execution_status: str
    runtime_target_id: UUID | None
    runtime_session_id: str | None
    cleanup_status: str
    operation: ExecutionOperationORM
    step_statuses: tuple[str, ...]
    attempts: tuple[ExecutionAttemptORM, ...]
    step_attempts: tuple[ExecutionStepAttemptORM, ...]
    artifacts: tuple[ExecutionArtifactORM, ...]
    outbox_events: tuple[ExecutionEventORM, ...]


@dataclass(frozen=True)
class CaseResult:
    name: str
    execution_id: str
    states: tuple[str, ...]
    attempt_statuses: tuple[str, ...]
    step_attempt_statuses: tuple[str, ...]
    event_types: tuple[str, ...]
    artifact_states: tuple[str, ...]


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _host_jupyter_endpoint(target: RuntimeTargetORM) -> str:
    variable = (
        "SINGLE_LIFECYCLE_JUPYTER_SECONDARY_ENDPOINT"
        if target.name == "local-jupyter-secondary"
        else "SINGLE_LIFECYCLE_JUPYTER_ENDPOINT"
    )
    default = (
        "http://127.0.0.1:8889"
        if target.name == "local-jupyter-secondary"
        else "http://127.0.0.1:8888"
    )
    return os.getenv(variable, default)


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


async def _wait_for_status(
    transport: Transport,
    execution_id: str,
    statuses: set[str],
    *,
    mcp: Client,
    rest: httpx.AsyncClient,
    timeout_seconds: float,
    require_runtime_session: bool = False,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    deadline = monotonic() + timeout_seconds
    observed: list[str] = []
    while monotonic() < deadline:
        execution = await _execution_get(
            transport, execution_id, mcp=mcp, rest=rest
        )
        status = execution["state"]["status"]
        if not observed or status != observed[-1]:
            observed.append(status)
        if status in statuses and (
            not require_runtime_session
            or execution["runtime"]["session_id"] is not None
        ):
            return execution, tuple(observed)
        await asyncio.sleep(0.1)
    raise RuntimeError(
        f"Execution {execution_id} did not reach {statuses} within the deadline."
    )


async def _page(
    transport: Transport,
    execution_id: str,
    kind: Literal["steps", "attempts", "events", "artifacts"],
    *,
    mcp: Client,
    rest: httpx.AsyncClient,
) -> list[dict[str, Any]]:
    limits = {"steps": 200, "attempts": 200, "events": 500, "artifacts": 1000}
    if transport == "MCP":
        tool = {
            "steps": "execution_step_list",
            "attempts": "execution_attempt_list",
            "events": "execution_event_list",
            "artifacts": "execution_artifact_list",
        }[kind]
        page = await _mcp_result(
            mcp,
            tool,
            {"execution_id": execution_id, "limit": limits[kind]},
        )
    else:
        page = await _rest_json(
            rest,
            "GET",
            f"/executions/{execution_id}/{kind}?limit={limits[kind]}",
        )
    if page["has_more"] or page["next_cursor"] is not None:
        raise RuntimeError(f"Unexpected pagination for {kind}: {page}")
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
                "limit": 200,
            },
        )
    else:
        page = await _rest_json(
            rest,
            "GET",
            f"/executions/{execution_id}/attempts/{attempt_id}/steps?limit=200",
        )
    return page["items"]


async def _database_snapshot(
    session_factory: async_sessionmaker[AsyncSession], execution_id: UUID
) -> DatabaseSnapshot:
    async with session_factory() as session:
        execution = await session.get(ExecutionORM, execution_id)
        if execution is None:
            raise RuntimeError(
                f"Execution {execution_id} is missing from PostgreSQL."
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
                .order_by(
                    ExecutionStepAttemptORM.created_at,
                    ExecutionStepAttemptORM.sequence,
                )
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
        outbox_events = list(
            await session.scalars(
                select(ExecutionEventORM)
                .where(ExecutionEventORM.execution_id == execution_id)
                .order_by(
                    ExecutionEventORM.created_at,
                    ExecutionEventORM.id,
                )
            )
        )
        operation = await session.scalar(
            select(ExecutionOperationORM).where(
                ExecutionOperationORM.execution_id == execution_id
            )
        )
        if operation is None:
            raise RuntimeError(f"Execution {execution_id} has no Operation.")
    return DatabaseSnapshot(
        execution_status=_enum_value(execution.status),
        runtime_target_id=execution.runtime_target_id,
        runtime_session_id=execution.runtime_session_id,
        cleanup_status=_enum_value(execution.runtime_session_cleanup_status),
        operation=operation,
        step_statuses=tuple(_enum_value(row.status) for row in steps),
        attempts=tuple(attempts),
        step_attempts=tuple(step_attempts),
        artifacts=tuple(artifacts),
        outbox_events=tuple(outbox_events),
    )


async def _wait_for_published_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    execution_id: UUID,
    timeout_seconds: float,
) -> DatabaseSnapshot:
    deadline = monotonic() + timeout_seconds
    while monotonic() < deadline:
        snapshot = await _database_snapshot(session_factory, execution_id)
        async with session_factory() as session:
            unpublished_count = await session.scalar(
                select(func.count(OutboxEventORM.id))
                .join(
                    ExecutionEventORM,
                    ExecutionEventORM.id == OutboxEventORM.execution_event_id,
                )
                .where(
                    ExecutionEventORM.execution_id == execution_id,
                    OutboxEventORM.published_at.is_(None),
                )
            )
        if snapshot.outbox_events and not unpublished_count:
            return snapshot
        await asyncio.sleep(0.1)
    raise RuntimeError(
        f"Outbox for Execution {execution_id} was not fully published."
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
        if fields.get("execution_id") == execution_id
    ]


async def _assert_event_delivery(
    transport: Transport,
    execution_id: str,
    snapshot: DatabaseSnapshot,
    *,
    mcp: Client,
    rest: httpx.AsyncClient,
    redis: Redis,
    stream: str,
    scan_limit: int,
) -> tuple[str, ...]:
    api_events = await _page(
        transport,
        execution_id,
        "events",
        mcp=mcp,
        rest=rest,
    )
    db_event_ids = {str(event.id) for event in snapshot.outbox_events}
    if {str(event["event_id"]) for event in api_events} != db_event_ids:
        raise RuntimeError("Event API and PostgreSQL Outbox event IDs differ.")
    if any(event["delivery"]["status"] != "PUBLISHED" for event in api_events):
        raise RuntimeError(
            f"Event API contains unpublished events: {api_events}"
        )
    redis_rows = await _redis_events(
        redis,
        stream,
        execution_id,
        scan_limit=scan_limit,
    )
    if {row["event_id"] for row in redis_rows} != db_event_ids:
        raise RuntimeError(
            "Redis Stream and PostgreSQL Outbox event IDs differ."
        )
    envelopes = [
        ExecutionStreamEnvelope.from_redis_fields(row) for row in redis_rows
    ]
    if any(
        envelope.schema_version != EXECUTION_EVENT_SCHEMA_VERSION
        for envelope in envelopes
    ):
        raise RuntimeError(
            "Redis Stream contains an unsupported event version."
        )
    return tuple(event.event_type for event in snapshot.outbox_events)


async def _runtime_session_exists(
    session_factory: async_sessionmaker[AsyncSession],
    target_id: UUID,
    runtime_session_id: str,
) -> bool:
    settings = get_settings()
    registry = RuntimeTargetRegistry(session_factory, settings)
    async with session_factory() as session:
        target = await session.get(RuntimeTargetORM, target_id)
    if target is None:
        raise RuntimeError(f"Runtime Target {target_id} is missing.")
    credential = registry.resolve_credential(
        target.credential_ref, target.credential_ciphertext
    )
    connection_config = dict(target.connection_config)
    connection_config["endpoint"] = _host_jupyter_endpoint(target)
    driver = ConfiguredRuntimeDriverFactory(settings).create(
        target.runtime_type,
        connection_config,
        credential,
    )
    try:
        return await driver.session_exists(runtime_session_id)
    finally:
        await driver.close()


async def _runtime_file_exists(
    session_factory: async_sessionmaker[AsyncSession],
    target_id: UUID,
    relative_path: str,
) -> bool:
    settings = get_settings()
    registry = RuntimeTargetRegistry(session_factory, settings)
    async with session_factory() as session:
        target = await session.get(RuntimeTargetORM, target_id)
    if target is None:
        raise RuntimeError(f"Runtime Target {target_id} is missing.")
    credential = registry.resolve_credential(
        target.credential_ref, target.credential_ciphertext
    )
    connection_config = dict(target.connection_config)
    connection_config["endpoint"] = _host_jupyter_endpoint(target)
    driver = ConfiguredRuntimeDriverFactory(settings).create(
        target.runtime_type, connection_config, credential
    )
    try:
        await driver.file_metadata(relative_path)
    except Exception:
        return False
    finally:
        await driver.close()
    return True


async def _run_failure_retry_case(
    *,
    unique: str,
    runtime_profile: str,
    timeout_seconds: float,
    scan_limit: int,
    mcp: Client,
    rest: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    stream: str,
) -> CaseResult:
    user_id = f"single-retry-{unique}-user"
    submitted = await _mcp_result(
        mcp,
        "execution_submit",
        {
            "request": execution_request(
                idempotency_key=f"single-retry-submit-{unique}",
                operation_mode="SINGLE",
                trigger_type="INTERACTIVE",
                actor={"type": "USER", "id": user_id},
                runtime_profile=runtime_profile,
                spec=inline_spec(
                    [
                        {
                            "skill_name": "data_io",
                            "tool_name": "initialize_retry_state",
                            "code": "attempt_counter = 0\nprint('initialized')",
                        },
                        {
                            "skill_name": "data_preprocess",
                            "tool_name": "fail_once_and_write_artifact",
                            "code": (
                                "from pathlib import Path\n"
                                "attempt_counter += 1\n"
                                "artifact = Path('artifacts/other/retry-e2e.txt')\n"
                                "artifact.write_text(str(attempt_counter), encoding='utf-8')\n"
                                "if attempt_counter == 1:\n"
                                "    raise RuntimeError('expected single retry failure')\n"
                                "print(attempt_counter)"
                            ),
                        },
                        {
                            "skill_name": "report",
                            "tool_name": "finish_retry",
                            "code": "print('retry completed')",
                        },
                    ],
                ),
                context={
                    "user_id": user_id,
                    "project_id": "single-retry-project",
                    "session_id": f"single-retry-session-{unique}",
                    "task_id": f"single-retry-task-{unique}",
                },
            )
        },
    )
    execution_id = str(submitted["execution_id"])
    operation_id = str(submitted["operation"]["operation_id"])
    failed, first_states = await _wait_for_status(
        "MCP",
        execution_id,
        {"FAILED"},
        mcp=mcp,
        rest=rest,
        timeout_seconds=timeout_seconds,
        require_runtime_session=True,
    )
    if (
        failed["failure"]["type"] != "TOOL_ERROR"
        or failed["retry"]["strategy"] != "FROM_FAILED_STEP"
        or failed["retry"]["from_sequence"] != 1
    ):
        raise RuntimeError(
            f"Execution did not expose a retained-kernel retry: {failed}"
        )
    retained_target_id = UUID(str(failed["runtime"]["target_id"]))
    retained_session_id = str(failed["runtime"]["session_id"])
    if not await _runtime_session_exists(
        session_factory, retained_target_id, retained_session_id
    ):
        raise RuntimeError(
            "Failed Execution did not retain its Jupyter session."
        )

    retried = await _mcp_result(
        mcp,
        "execution_retry",
        {
            "request": {
                "execution_id": execution_id,
                "idempotency_key": f"single-retry-command-{unique}",
                "actor": {"type": "USER", "id": user_id},
            }
        },
    )
    if retried["state"]["status"] != "QUEUED":
        raise RuntimeError(f"Retry was not queued: {retried}")
    if retried["operation"]["operation_id"] != operation_id:
        raise RuntimeError(
            "Retry did not return the originally accepted Operation ID."
        )
    succeeded, second_states = await _wait_for_status(
        "MCP",
        execution_id,
        TERMINAL_STATUSES,
        mcp=mcp,
        rest=rest,
        timeout_seconds=timeout_seconds,
    )
    if (
        succeeded["state"]["status"] != "SUCCEEDED"
        or succeeded["runtime"]["target_id"] != str(retained_target_id)
        or succeeded["runtime"]["session_id"] is not None
        or succeeded["retry"]["count"] != 1
        or succeeded["recovery"]["runtime_session_cleanup_status"]
        != "SUCCEEDED"
    ):
        raise RuntimeError(
            f"Retained-kernel retry did not succeed cleanly: {succeeded}"
        )
    if await _runtime_session_exists(
        session_factory, retained_target_id, retained_session_id
    ):
        raise RuntimeError(
            "Successful retry leaked its retained Jupyter session."
        )

    snapshot = await _wait_for_published_snapshot(
        session_factory, UUID(execution_id), timeout_seconds
    )
    if (
        snapshot.execution_status != "SUCCEEDED"
        or snapshot.runtime_session_id is not None
        or snapshot.cleanup_status != "SUCCEEDED"
        or snapshot.step_statuses != ("SUCCEEDED", "SUCCEEDED", "SUCCEEDED")
        or tuple(_enum_value(attempt.status) for attempt in snapshot.attempts)
        != ("FAILED", "SUCCEEDED")
        or str(snapshot.operation.id) != operation_id
        or _enum_value(snapshot.operation.status) != "SUCCEEDED"
        or snapshot.operation.execution_attempt_id != snapshot.attempts[1].id
    ):
        raise RuntimeError(
            f"PostgreSQL retry state is inconsistent: {snapshot}"
        )
    if (
        snapshot.attempts[0].runtime_session_id != retained_session_id
        or snapshot.attempts[1].runtime_session_id != retained_session_id
        or snapshot.attempts[0].runtime_target_id != retained_target_id
        or snapshot.attempts[1].runtime_target_id != retained_target_id
    ):
        raise RuntimeError(
            "Retry Attempt history did not retain the original Runtime identity."
        )
    first_attempt_steps = tuple(
        _enum_value(step.status)
        for step in snapshot.step_attempts
        if step.execution_attempt_id == snapshot.attempts[0].id
    )
    second_attempt_steps = tuple(
        _enum_value(step.status)
        for step in snapshot.step_attempts
        if step.execution_attempt_id == snapshot.attempts[1].id
    )
    if first_attempt_steps != (
        "SUCCEEDED",
        "FAILED",
    ) or second_attempt_steps != (
        "SUCCEEDED",
        "SUCCEEDED",
    ):
        raise RuntimeError(
            f"Retry Step Attempt history is inconsistent: {snapshot.step_attempts}"
        )

    api_steps = await _page("MCP", execution_id, "steps", mcp=mcp, rest=rest)
    api_attempts = await _page(
        "MCP", execution_id, "attempts", mcp=mcp, rest=rest
    )
    if [step["result"]["status"] for step in api_steps] != [
        "SUCCEEDED",
        "SUCCEEDED",
        "SUCCEEDED",
    ] or [attempt["state"]["status"] for attempt in api_attempts] != [
        "FAILED",
        "SUCCEEDED",
    ]:
        raise RuntimeError("MCP retry history differs from PostgreSQL.")
    for expected, attempt in zip(
        (("SUCCEEDED", "FAILED"), ("SUCCEEDED", "SUCCEEDED")),
        api_attempts,
        strict=True,
    ):
        attempt_id = str(attempt["attempt_id"])
        detail = await _attempt_detail(
            "MCP", execution_id, attempt_id, mcp=mcp, rest=rest
        )
        history = await _attempt_steps(
            "MCP", execution_id, attempt_id, mcp=mcp, rest=rest
        )
        if (
            detail["runtime"]["session_id"] != retained_session_id
            or tuple(step["result"]["status"] for step in history) != expected
        ):
            raise RuntimeError(
                "MCP Attempt detail or Step history is inconsistent."
            )

    artifacts = [
        artifact
        for artifact in snapshot.artifacts
        if artifact.name == "retry-e2e.txt"
    ]
    if (
        tuple(_enum_value(artifact.status) for artifact in artifacts)
        != (
            "INCOMPLETE",
            "AVAILABLE",
        )
        or artifacts[0].execution_attempt_id
        == artifacts[1].execution_attempt_id
    ):
        raise RuntimeError(
            f"Retry Artifact history is inconsistent: {artifacts}"
        )
    expected_checksums = tuple(
        hashlib.sha256(value.encode()).hexdigest() for value in ("1", "2")
    )
    if (
        tuple(artifact.checksum_sha256 for artifact in artifacts)
        != expected_checksums
    ):
        raise RuntimeError(
            "Retry Artifact checksums do not preserve both write versions."
        )
    notebook = next(
        (
            artifact
            for artifact in snapshot.artifacts
            if _enum_value(artifact.artifact_type) == "NOTEBOOK"
        ),
        None,
    )
    if notebook is None:
        raise RuntimeError("Successful retry notebook Artifact is missing.")
    notebook_view = await _mcp_result(
        mcp,
        "execution_notebook_read",
        {
            "execution_id": execution_id,
            "view": "FULL",
            "limit": 200,
        },
    )
    if notebook_view["page"]["total_count"] != 3:
        raise RuntimeError(
            f"Successful retry notebook is incomplete: {notebook_view}"
        )
    event_types = await _assert_event_delivery(
        "MCP",
        execution_id,
        snapshot,
        mcp=mcp,
        rest=rest,
        redis=redis,
        stream=stream,
        scan_limit=scan_limit,
    )
    required_events = {
        "execution.started",
        "execution.operation_started",
        "execution.step_started",
        "execution.step_completed",
        "execution.operation_completed",
        "execution.completed",
    }
    if not required_events.issubset(event_types):
        raise RuntimeError(
            f"Retry event timeline is incomplete: {event_types}"
        )
    operation_events = [
        event
        for event in snapshot.outbox_events
        if event.event_type == "execution.operation_completed"
    ]
    if [event.payload["status"] for event in operation_events] != [
        "FAILED",
        "SUCCEEDED",
    ]:
        raise RuntimeError(
            f"Operation retry event order is inconsistent: {operation_events}"
        )
    if any(
        event.payload["operation"]["id"] != operation_id
        for event in operation_events
    ):
        raise RuntimeError(
            "Operation retry events do not share the accepted Operation ID."
        )
    first_attempt_ids = {
        result["attempt"]["id"]
        for result in operation_events[0].payload["step_results"]
    }
    retried_attempt_ids = {
        result["attempt"]["id"]
        for result in operation_events[1].payload["step_results"]
    }
    if first_attempt_ids != {str(snapshot.attempts[0].id)} or (
        retried_attempt_ids
        != {str(snapshot.attempts[0].id), str(snapshot.attempts[1].id)}
    ):
        raise RuntimeError(
            "Operation retry events do not preserve successful results from "
            "the first Attempt alongside retried Step results."
        )
    return CaseResult(
        name="MCP failure -> retry",
        execution_id=execution_id,
        states=(*first_states, *second_states),
        attempt_statuses=tuple(
            _enum_value(row.status) for row in snapshot.attempts
        ),
        step_attempt_statuses=tuple(
            _enum_value(row.status) for row in snapshot.step_attempts
        ),
        event_types=event_types,
        artifact_states=tuple(_enum_value(row.status) for row in artifacts),
    )


async def _run_cancel_case(
    *,
    unique: str,
    runtime_profile: str,
    timeout_seconds: float,
    scan_limit: int,
    mcp: Client,
    rest: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    stream: str,
) -> CaseResult:
    user_id = f"single-cancel-{unique}-user"
    payload = execution_request(
        idempotency_key=f"single-cancel-submit-{unique}",
        operation_mode="SINGLE",
        trigger_type="INTERACTIVE",
        actor={"type": "USER", "id": user_id},
        runtime_profile=runtime_profile,
        spec=inline_spec(
            [
                {
                    "skill_name": "report",
                    "tool_name": "write_then_wait",
                    "code": (
                        "from pathlib import Path\n"
                        "import time\n"
                        "marker = Path('artifacts/other/cancel-e2e.txt')\n"
                        "marker.write_text('started', encoding='utf-8')\n"
                        "print('cancel marker written', flush=True)\n"
                        "time.sleep(120)\n"
                        "marker.write_text('unexpected-finish', encoding='utf-8')"
                    ),
                },
                {
                    "skill_name": "report",
                    "tool_name": "must_not_run",
                    "code": "raise RuntimeError('cancelled execution ran a later step')",
                },
            ],
        ),
        context={
            "user_id": user_id,
            "project_id": "single-cancel-project",
            "session_id": f"single-cancel-session-{unique}",
            "task_id": f"single-cancel-task-{unique}",
        },
    )
    submitted = await _rest_json(rest, "POST", "/executions", json=payload)
    execution_id = str(submitted["execution_id"])
    running, running_states = await _wait_for_status(
        "REST",
        execution_id,
        {"RUNNING"},
        mcp=mcp,
        rest=rest,
        timeout_seconds=timeout_seconds,
        require_runtime_session=True,
    )
    runtime_target_id = UUID(str(running["runtime"]["target_id"]))
    runtime_session_id = str(running["runtime"]["session_id"])
    workspace_path = running["workspace"]["path"]
    if not workspace_path:
        raise RuntimeError("Running Execution has no Runtime workspace path.")
    marker_relative = f"{workspace_path}/artifacts/other/cancel-e2e.txt"
    deadline = monotonic() + timeout_seconds
    marker_poll = asyncio.Event()
    marker_exists = False
    while monotonic() < deadline:
        marker_exists = await _runtime_file_exists(
            session_factory, runtime_target_id, marker_relative
        )
        if marker_exists:
            break
        try:
            await asyncio.wait_for(marker_poll.wait(), timeout=0.2)
        except TimeoutError:
            pass
    if not marker_exists:
        raise RuntimeError(
            "Cancellation marker was not written before the deadline."
        )

    cancel_requested = await _rest_json(
        rest,
        "POST",
        f"/executions/{execution_id}/cancel",
        json={
            "idempotency_key": f"single-cancel-command-{unique}",
            "reason": "single cancellation regression E2E",
            "actor": {"type": "USER", "id": user_id},
        },
    )
    if cancel_requested["state"]["status"] != "CANCEL_REQUESTED":
        raise RuntimeError(
            f"REST cancellation request was not accepted: {cancel_requested}"
        )
    cancelled, terminal_states = await _wait_for_status(
        "REST",
        execution_id,
        {"CANCELLED"},
        mcp=mcp,
        rest=rest,
        timeout_seconds=timeout_seconds,
    )
    if (
        cancelled["state"]["cancellation_reason"]
        != "single cancellation regression E2E"
        or cancelled["runtime"]["session_id"] is not None
        or cancelled["retry"]["strategy"] != "NOT_RETRYABLE"
        or cancelled["recovery"]["runtime_session_cleanup_status"]
        != "SUCCEEDED"
    ):
        raise RuntimeError(
            f"REST cancellation did not clean up safely: {cancelled}"
        )
    if await _runtime_session_exists(
        session_factory, runtime_target_id, runtime_session_id
    ):
        raise RuntimeError("Cancelled Execution leaked its Jupyter session.")

    snapshot = await _wait_for_published_snapshot(
        session_factory, UUID(execution_id), timeout_seconds
    )
    if (
        snapshot.execution_status != "CANCELLED"
        or snapshot.runtime_session_id is not None
        or snapshot.cleanup_status != "SUCCEEDED"
        or snapshot.step_statuses != ("CANCELLED", "CANCELLED")
        or tuple(_enum_value(attempt.status) for attempt in snapshot.attempts)
        != ("CANCELLED",)
        or tuple(_enum_value(step.status) for step in snapshot.step_attempts)
        != ("CANCELLED",)
    ):
        raise RuntimeError(
            f"PostgreSQL cancellation state is inconsistent: {snapshot}"
        )
    api_steps = await _page("REST", execution_id, "steps", mcp=mcp, rest=rest)
    api_attempts = await _page(
        "REST", execution_id, "attempts", mcp=mcp, rest=rest
    )
    if [step["result"]["status"] for step in api_steps] != [
        "CANCELLED",
        "CANCELLED",
    ] or [attempt["state"]["status"] for attempt in api_attempts] != [
        "CANCELLED"
    ]:
        raise RuntimeError(
            "REST cancellation history differs from PostgreSQL."
        )
    attempt_id = str(api_attempts[0]["attempt_id"])
    attempt_detail = await _attempt_detail(
        "REST", execution_id, attempt_id, mcp=mcp, rest=rest
    )
    attempt_history = await _attempt_steps(
        "REST", execution_id, attempt_id, mcp=mcp, rest=rest
    )
    if (
        attempt_detail["runtime"]["session_id"] != runtime_session_id
        or attempt_detail["recovery"]["runtime_session_cleanup_status"]
        != "SUCCEEDED"
        or [step["result"]["status"] for step in attempt_history]
        != ["CANCELLED"]
    ):
        raise RuntimeError("REST cancelled Attempt detail is inconsistent.")

    cancel_artifacts = [
        artifact
        for artifact in snapshot.artifacts
        if artifact.name == "cancel-e2e.txt"
    ]
    if tuple(
        _enum_value(artifact.status) for artifact in cancel_artifacts
    ) != ("INCOMPLETE",):
        raise RuntimeError(
            f"Cancelled-cell Artifact was not preserved: {cancel_artifacts}"
        )
    expected_checksum = hashlib.sha256(b"started").hexdigest()
    if cancel_artifacts[0].checksum_sha256 != expected_checksum:
        raise RuntimeError("Cancelled-cell Artifact content is invalid.")
    if any(
        _enum_value(artifact.artifact_type) == "NOTEBOOK"
        for artifact in snapshot.artifacts
    ):
        raise RuntimeError(
            "Cancelled Execution unexpectedly registered a successful notebook."
        )
    event_types = await _assert_event_delivery(
        "REST",
        execution_id,
        snapshot,
        mcp=mcp,
        rest=rest,
        redis=redis,
        stream=stream,
        scan_limit=scan_limit,
    )
    required_events = {
        "execution.started",
        "execution.operation_started",
        "execution.step_started",
        "execution.step_completed",
        "execution.operation_completed",
        "execution.completed",
    }
    if not required_events.issubset(event_types):
        raise RuntimeError(
            f"Cancellation event timeline is incomplete: {event_types}"
        )
    return CaseResult(
        name="REST running cancel",
        execution_id=execution_id,
        states=(*running_states, "CANCEL_REQUESTED", *terminal_states),
        attempt_statuses=tuple(
            _enum_value(row.status) for row in snapshot.attempts
        ),
        step_attempt_statuses=tuple(
            _enum_value(row.status) for row in snapshot.step_attempts
        ),
        event_types=event_types,
        artifact_states=tuple(
            _enum_value(row.status) for row in cancel_artifacts
        ),
    )


async def main() -> None:
    settings = get_settings()
    mcp_url = os.getenv("EXECUTOR_MCP_URL", "http://127.0.0.1:8000/mcp")
    rest_url = os.getenv("EXECUTOR_REST_URL", "http://127.0.0.1:8000/api/v1")
    runtime_profile = os.getenv("SINGLE_LIFECYCLE_RUNTIME_PROFILE", "default")
    timeout_seconds = float(
        os.getenv("SINGLE_LIFECYCLE_TIMEOUT_SECONDS", "120")
    )
    scan_limit = int(os.getenv("SINGLE_LIFECYCLE_STREAM_SCAN_LIMIT", "5000"))
    unique = uuid4().hex
    engine = create_engine(
        settings.database_dsn,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout_seconds=settings.database_pool_timeout_seconds,
        pool_recycle_seconds=settings.database_pool_recycle_seconds,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    session_factory = create_session_factory(engine)
    redis = Redis.from_url(settings.redis_dsn, decode_responses=True)
    try:
        async with (
            Client(mcp_url) as mcp,
            httpx.AsyncClient(
                base_url=f"{rest_url.rstrip('/')}/", timeout=30
            ) as rest,
        ):
            results = [
                await _run_failure_retry_case(
                    unique=unique,
                    runtime_profile=runtime_profile,
                    timeout_seconds=timeout_seconds,
                    scan_limit=scan_limit,
                    mcp=mcp,
                    rest=rest,
                    session_factory=session_factory,
                    redis=redis,
                    stream=settings.redis_event_stream,
                ),
                await _run_cancel_case(
                    unique=unique,
                    runtime_profile=runtime_profile,
                    timeout_seconds=timeout_seconds,
                    scan_limit=scan_limit,
                    mcp=mcp,
                    rest=rest,
                    session_factory=session_factory,
                    redis=redis,
                    stream=settings.redis_event_stream,
                ),
            ]
        for result in results:
            print(f"[{result.name}]")
            print("execution_id:", result.execution_id)
            print("states:", " -> ".join(result.states))
            print("attempt_statuses:", list(result.attempt_statuses))
            print("step_attempt_statuses:", list(result.step_attempt_statuses))
            print("artifact_statuses:", list(result.artifact_states))
            print("outbox_and_redis_events:", list(result.event_types))
    finally:
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
