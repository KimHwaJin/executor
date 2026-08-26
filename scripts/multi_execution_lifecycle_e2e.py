"""Exercise MULTI correction, finalization, and cancellation across durable boundaries."""

import asyncio
import hashlib
import json
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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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


@dataclass(frozen=True)
class DatabaseSnapshot:
    execution_status: str
    runtime_target_id: UUID | None
    runtime_session_id: str | None
    cleanup_status: str
    steps: tuple[ExecutionStepORM, ...]
    attempts: tuple[ExecutionAttemptORM, ...]
    step_attempts: tuple[ExecutionStepAttemptORM, ...]
    artifacts: tuple[ExecutionArtifactORM, ...]
    outbox_events: tuple[OutboxEventORM, ...]


@dataclass(frozen=True)
class CaseResult:
    name: str
    execution_id: str
    statuses: tuple[str, ...]
    step_statuses: tuple[str, ...]
    artifact_statuses: tuple[str, ...]
    event_types: tuple[str, ...]


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _host_jupyter_endpoint(target: RuntimeTargetORM) -> str:
    variable = (
        "MULTI_LIFECYCLE_JUPYTER_SECONDARY_ENDPOINT"
        if target.name == "local-jupyter-secondary"
        else "MULTI_LIFECYCLE_JUPYTER_ENDPOINT"
    )
    default = (
        "http://127.0.0.1:8889"
        if target.name == "local-jupyter-secondary"
        else "http://127.0.0.1:8888"
    )
    return os.getenv(variable, default)


def _single_step_spec(
    sequence: int,
    *,
    skill_name: str,
    tool_name: str,
    code: str,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "steps": [
            {
                "sequence": sequence,
                "payload": {
                    "type": "PYTHON_EXECUTE",
                    "source": {"type": "INLINE", "content": code},
                },
                "lineage": {
                    "skill_name": skill_name,
                    "tool_name": tool_name,
                    "input_parameters": {},
                },
            }
        ],
    }


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
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = await client.request(method, path.lstrip("/"), json=json_body)
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
        if not observed or observed[-1] != status:
            observed.append(status)
        if status in statuses and (
            not require_runtime_session
            or execution["runtime"]["session_id"] is not None
        ):
            return execution, tuple(observed)
        await asyncio.sleep(0.1)
    raise RuntimeError(
        f"Execution {execution_id} did not reach {statuses} before timeout."
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
        steps = tuple(
            await session.scalars(
                select(ExecutionStepORM)
                .where(ExecutionStepORM.execution_id == execution_id)
                .order_by(ExecutionStepORM.sequence)
            )
        )
        attempts = tuple(
            await session.scalars(
                select(ExecutionAttemptORM)
                .where(ExecutionAttemptORM.execution_id == execution_id)
                .order_by(ExecutionAttemptORM.attempt_number)
            )
        )
        step_attempts = tuple(
            await session.scalars(
                select(ExecutionStepAttemptORM)
                .where(ExecutionStepAttemptORM.execution_id == execution_id)
                .order_by(ExecutionStepAttemptORM.sequence)
            )
        )
        artifacts = tuple(
            await session.scalars(
                select(ExecutionArtifactORM)
                .where(ExecutionArtifactORM.execution_id == execution_id)
                .order_by(
                    ExecutionArtifactORM.created_at, ExecutionArtifactORM.id
                )
            )
        )
        outbox_events = tuple(
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
        runtime_target_id=execution.runtime_target_id,
        runtime_session_id=execution.runtime_session_id,
        cleanup_status=_enum_value(execution.runtime_session_cleanup_status),
        steps=steps,
        attempts=attempts,
        step_attempts=step_attempts,
        artifacts=artifacts,
        outbox_events=outbox_events,
    )


async def _wait_for_published_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    execution_id: UUID,
    timeout_seconds: float,
) -> DatabaseSnapshot:
    deadline = monotonic() + timeout_seconds
    while monotonic() < deadline:
        snapshot = await _database_snapshot(session_factory, execution_id)
        if snapshot.outbox_events and all(
            _enum_value(event.status) == "PUBLISHED"
            for event in snapshot.outbox_events
        ):
            return snapshot
        await asyncio.sleep(0.1)
    raise RuntimeError(
        f"Outbox for Execution {execution_id} was not fully published."
    )


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
        transport, execution_id, "events", mcp=mcp, rest=rest
    )
    db_event_ids = {str(event.id) for event in snapshot.outbox_events}
    if {str(event["event_id"]) for event in api_events} != db_event_ids:
        raise RuntimeError(
            "Public Event history and PostgreSQL Outbox IDs differ."
        )
    redis_rows = [
        fields
        for _, fields in await redis.xrevrange(stream, count=scan_limit)
        if fields.get("aggregate_id") == execution_id
    ]
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
            "Redis Stream contains an unsupported Execution event version."
        )
    if any(
        event.payload.get("schema_version") != EXECUTION_EVENT_SCHEMA_VERSION
        for event in snapshot.outbox_events
    ):
        raise RuntimeError(
            "PostgreSQL Outbox contains an unsupported Execution event version."
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


async def _assert_waiting_runtime(
    execution: dict[str, Any],
    *,
    expected_target_id: UUID | None,
    expected_session_id: str | None,
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[UUID, str]:
    if execution["state"]["status"] != "WAITING_FOR_OPERATION":
        raise RuntimeError(f"MULTI Execution is not waiting: {execution}")
    target_id = UUID(str(execution["runtime"]["target_id"]))
    session_id = str(execution["runtime"]["session_id"])
    if expected_target_id is not None and target_id != expected_target_id:
        raise RuntimeError(
            "MULTI Execution changed Runtime Target between cells."
        )
    if expected_session_id is not None and session_id != expected_session_id:
        raise RuntimeError(
            "MULTI Execution changed Runtime session between cells."
        )
    if not await _runtime_session_exists(
        session_factory, target_id, session_id
    ):
        raise RuntimeError("MULTI waiting session is missing from Jupyter.")
    return target_id, session_id


async def _run_correction_and_finalization_case(
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
    user_id = f"multi-flow-{unique}-user"
    actor = {"type": "USER", "id": user_id}
    submitted = await _mcp_result(
        mcp,
        "execution_submit",
        {
            "request": execution_request(
                idempotency_key=f"multi-flow-submit-{unique}",
                operation_mode="MULTI",
                operation_wait_timeout_seconds=600,
                trigger_type="INTERACTIVE",
                runtime_profile=runtime_profile,
                actor=actor,
                spec=inline_spec(
                    [
                        {
                            "skill_name": "data_load",
                            "tool_name": "initialize_state",
                            "code": "initial_runs = 1\nvalue = 40\nprint(value)",
                        }
                    ],
                ),
                context={
                    "user_id": user_id,
                    "project_id": "multi-flow-project",
                    "session_id": f"multi-flow-session-{unique}",
                    "task_id": f"multi-flow-task-{unique}",
                },
            )
        },
    )
    execution_id = str(submitted["execution_id"])
    first, first_states = await _wait_for_status(
        "MCP",
        execution_id,
        {"WAITING_FOR_OPERATION"},
        mcp=mcp,
        rest=rest,
        timeout_seconds=timeout_seconds,
        require_runtime_session=True,
    )
    target_id, session_id = await _assert_waiting_runtime(
        first,
        expected_target_id=None,
        expected_session_id=None,
        session_factory=session_factory,
    )

    operation_created = await _rest_json(
        rest,
        "POST",
        f"/executions/{execution_id}/operations",
        json_body={
            "idempotency_key": f"multi-flow-continue-1-{unique}",
            "expected_version": first["state"]["version"],
            "actor": actor,
            "spec": _single_step_spec(
                1,
                skill_name="eda",
                tool_name="calculate_answer",
                code=(
                    "from pathlib import Path\n"
                    "answer = value + 2\n"
                    "Path('artifacts/other/multi-answer.txt').write_text(\n"
                    "    str(answer), encoding='utf-8'\n"
                    ")\n"
                    "print(answer)"
                ),
            ),
        },
    )
    if operation_created["state"]["status"] != "QUEUED":
        raise RuntimeError(
            f"REST MULTI Operation was not queued: {operation_created}"
        )
    second, second_states = await _wait_for_status(
        "REST",
        execution_id,
        {"WAITING_FOR_OPERATION"},
        mcp=mcp,
        rest=rest,
        timeout_seconds=timeout_seconds,
        require_runtime_session=True,
    )
    await _assert_waiting_runtime(
        second,
        expected_target_id=target_id,
        expected_session_id=session_id,
        session_factory=session_factory,
    )

    failed_command = await _mcp_result(
        mcp,
        "execution_operation_create",
        {
            "request": {
                "execution_id": execution_id,
                "idempotency_key": f"multi-flow-continue-2-{unique}",
                "expected_version": second["state"]["version"],
                "actor": actor,
                "spec": _single_step_spec(
                    2,
                    skill_name="modeling",
                    tool_name="planned_failure",
                    code=(
                        "from pathlib import Path\n"
                        "Path('artifacts/other/multi-failed.txt').write_text(\n"
                        "    'partial', encoding='utf-8'\n"
                        ")\n"
                        "raise RuntimeError('planned multi correction')"
                    ),
                ),
            }
        },
    )
    if failed_command["state"]["status"] != "QUEUED":
        raise RuntimeError(
            f"MCP MULTI failure step was not queued: {failed_command}"
        )
    failed, failed_states = await _wait_for_status(
        "MCP",
        execution_id,
        {"WAITING_FOR_OPERATION"},
        mcp=mcp,
        rest=rest,
        timeout_seconds=timeout_seconds,
        require_runtime_session=True,
    )
    await _assert_waiting_runtime(
        failed,
        expected_target_id=target_id,
        expected_session_id=session_id,
        session_factory=session_factory,
    )
    failed_steps = await _page(
        "MCP", execution_id, "steps", mcp=mcp, rest=rest
    )
    if [step["result"]["status"] for step in failed_steps] != [
        "SUCCEEDED",
        "SUCCEEDED",
        "FAILED",
    ]:
        raise RuntimeError(
            f"MULTI failure did not remain append-only: {failed_steps}"
        )

    corrected_command = await _rest_json(
        rest,
        "POST",
        f"/executions/{execution_id}/operations",
        json_body={
            "idempotency_key": f"multi-flow-continue-3-{unique}",
            "expected_version": failed["state"]["version"],
            "actor": actor,
            "spec": _single_step_spec(
                3,
                skill_name="evaluation",
                tool_name="correct_failure",
                code=(
                    "from pathlib import Path\n"
                    "assert initial_runs == 1\n"
                    "assert answer == 42\n"
                    "corrected = answer * 2\n"
                    "Path('artifacts/reports/multi-corrected.txt').write_text(\n"
                    "    str(corrected), encoding='utf-8'\n"
                    ")\n"
                    "print(corrected)"
                ),
            ),
        },
    )
    if corrected_command["state"]["status"] != "QUEUED":
        raise RuntimeError(
            f"REST corrected step was not queued: {corrected_command}"
        )
    corrected, corrected_states = await _wait_for_status(
        "REST",
        execution_id,
        {"WAITING_FOR_OPERATION"},
        mcp=mcp,
        rest=rest,
        timeout_seconds=timeout_seconds,
        require_runtime_session=True,
    )
    await _assert_waiting_runtime(
        corrected,
        expected_target_id=target_id,
        expected_session_id=session_id,
        session_factory=session_factory,
    )

    finalization_requested = await _mcp_result(
        mcp,
        "execution_finalize",
        {
            "request": {
                "execution_id": execution_id,
                "idempotency_key": f"multi-flow-finish-{unique}",
                "expected_version": corrected["state"]["version"],
                "actor": actor,
            }
        },
    )
    if finalization_requested["state"]["status"] != "FINALIZING":
        raise RuntimeError(
            f"MCP MULTI finalization was not queued: {finalization_requested}"
        )
    finished, finished_states = await _wait_for_status(
        "MCP",
        execution_id,
        {"SUCCEEDED", "FAILED", "CANCELLED"},
        mcp=mcp,
        rest=rest,
        timeout_seconds=timeout_seconds,
    )
    if (
        finished["state"]["status"] != "SUCCEEDED"
        or finished["runtime"]["target_id"] != str(target_id)
        or finished["runtime"]["session_id"] is not None
        or finished["recovery"]["runtime_session_cleanup_status"]
        != "SUCCEEDED"
    ):
        raise RuntimeError(
            f"MULTI finalization did not clean up safely: {finished}"
        )
    if await _runtime_session_exists(session_factory, target_id, session_id):
        raise RuntimeError(
            "Finished MULTI Execution leaked its Jupyter session."
        )

    snapshot = await _wait_for_published_snapshot(
        session_factory, UUID(execution_id), timeout_seconds
    )
    step_statuses = tuple(_enum_value(step.status) for step in snapshot.steps)
    if (
        snapshot.execution_status != "SUCCEEDED"
        or snapshot.runtime_session_id is not None
        or snapshot.cleanup_status != "SUCCEEDED"
        or step_statuses != ("SUCCEEDED", "SUCCEEDED", "FAILED", "SUCCEEDED")
        or tuple(_enum_value(attempt.status) for attempt in snapshot.attempts)
        != ("SUCCEEDED",)
        or tuple(_enum_value(step.status) for step in snapshot.step_attempts)
        != ("SUCCEEDED", "SUCCEEDED", "FAILED", "SUCCEEDED")
    ):
        raise RuntimeError(
            f"PostgreSQL MULTI history is inconsistent: {snapshot}"
        )
    attempt = snapshot.attempts[0]
    if (
        attempt.runtime_target_id != target_id
        or attempt.runtime_session_id != session_id
    ):
        raise RuntimeError(
            "MULTI Attempt lost its historical Runtime identity."
        )

    for transport in ("REST", "MCP"):
        api_steps = await _page(
            transport, execution_id, "steps", mcp=mcp, rest=rest
        )
        api_attempts = await _page(
            transport, execution_id, "attempts", mcp=mcp, rest=rest
        )
        if (
            tuple(step["result"]["status"] for step in api_steps)
            != step_statuses
        ):
            raise RuntimeError(
                f"{transport} Step history differs from PostgreSQL."
            )
        if [item["state"]["status"] for item in api_attempts] != ["SUCCEEDED"]:
            raise RuntimeError(
                f"{transport} Attempt history differs from PostgreSQL."
            )
        attempt_id = str(api_attempts[0]["attempt_id"])
        detail = await _attempt_detail(
            transport, execution_id, attempt_id, mcp=mcp, rest=rest
        )
        history = await _attempt_steps(
            transport, execution_id, attempt_id, mcp=mcp, rest=rest
        )
        if (
            detail["runtime"]["session_id"] != session_id
            or tuple(step["result"]["status"] for step in history)
            != step_statuses
        ):
            raise RuntimeError(
                f"{transport} immutable Attempt history is inconsistent."
            )

    named_artifacts = {
        artifact.name: artifact
        for artifact in snapshot.artifacts
        if artifact.name
        in {"multi-answer.txt", "multi-failed.txt", "multi-corrected.txt"}
    }
    expected_artifacts = {
        "multi-answer.txt": ("AVAILABLE", "42"),
        "multi-failed.txt": ("INCOMPLETE", "partial"),
        "multi-corrected.txt": ("AVAILABLE", "84"),
    }
    for name, (status, content) in expected_artifacts.items():
        artifact = named_artifacts.get(name)
        if artifact is None or _enum_value(artifact.status) != status:
            raise RuntimeError(
                f"MULTI Artifact {name} is missing or has the wrong status."
            )
        if (
            artifact.checksum_sha256
            != hashlib.sha256(content.encode()).hexdigest()
        ):
            raise RuntimeError(
                f"MULTI Artifact {name} has unexpected content."
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
        raise RuntimeError(
            "Finished MULTI Execution has no notebook Artifact."
        )
    notebook_data = await _mcp_result(
        mcp,
        "execution_notebook_read",
        {
            "execution_id": execution_id,
            "view": "FULL",
            "limit": 200,
        },
    )
    notebook_source = "\n".join(
        cell["source"] for cell in notebook_data["cells"]
    )
    if notebook_data["page"]["total_count"] != 4 or not all(
        marker in notebook_source
        for marker in (
            "value = 40",
            "answer = value + 2",
            "planned multi correction",
            "corrected = answer * 2",
        )
    ):
        raise RuntimeError(
            "MULTI notebook does not preserve correction history."
        )
    detailed_cells = [
        await _rest_json(
            rest, "GET", f"/executions/{execution_id}/notebook/cells/{index}"
        )
        for index in range(4)
    ]
    detailed_outputs = json.dumps(
        [item["cell"]["outputs"] for item in detailed_cells]
    )
    if not all(
        marker in detailed_outputs
        for marker in ("40", "42", "planned multi correction", "84")
    ):
        raise RuntimeError("MULTI notebook does not preserve cell outputs.")

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
        "execution.submitted",
        "execution.started",
        "execution.operation_submitted",
        "execution.operation_succeeded",
        "execution.operation_failed",
        "execution.finalization_requested",
        "execution.succeeded",
        "execution.artifact_registered",
    }
    if not required_events.issubset(event_types):
        raise RuntimeError(
            f"MULTI completion event timeline is incomplete: {event_types}"
        )
    return CaseResult(
        name="REST/MCP correction -> finalization",
        execution_id=execution_id,
        statuses=(
            *first_states,
            *second_states,
            *failed_states,
            *corrected_states,
            *finished_states,
        ),
        step_statuses=step_statuses,
        artifact_statuses=tuple(
            _enum_value(named_artifacts[name].status)
            for name in expected_artifacts
        ),
        event_types=event_types,
    )


async def _run_running_cancel_case(
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
    user_id = f"multi-cancel-{unique}-user"
    project_id = "multi-cancel-project"
    session_id = f"multi-cancel-session-{unique}"
    actor = {"type": "USER", "id": user_id}
    submitted = await _rest_json(
        rest,
        "POST",
        "/executions",
        json_body=execution_request(
            idempotency_key=f"multi-cancel-submit-{unique}",
            operation_mode="MULTI",
            operation_wait_timeout_seconds=600,
            trigger_type="INTERACTIVE",
            runtime_profile=runtime_profile,
            actor=actor,
            spec=inline_spec(
                [
                    {
                        "skill_name": "report",
                        "tool_name": "write_then_wait",
                        "code": (
                            "from pathlib import Path\n"
                            "import time\n"
                            "marker = Path('artifacts/other/multi-cancel.txt')\n"
                            "marker.write_text('started', encoding='utf-8')\n"
                            "print('multi cancel marker written', flush=True)\n"
                            "time.sleep(120)\n"
                            "marker.write_text('unexpected-finish', encoding='utf-8')"
                        ),
                    }
                ],
            ),
            context={
                "user_id": user_id,
                "project_id": project_id,
                "session_id": session_id,
                "task_id": f"multi-cancel-task-{unique}",
            },
        ),
    )
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
    target_id = UUID(str(running["runtime"]["target_id"]))
    runtime_session_id = str(running["runtime"]["session_id"])
    workspace_path = running["workspace"]["path"]
    if not workspace_path:
        raise RuntimeError(
            "Running MULTI Execution has no Runtime workspace path."
        )
    marker_path = f"{workspace_path}/artifacts/other/multi-cancel.txt"
    deadline = monotonic() + timeout_seconds
    marker_poll = asyncio.Event()
    marker_exists = False
    while monotonic() < deadline:
        marker_exists = await _runtime_file_exists(
            session_factory, target_id, marker_path
        )
        if marker_exists:
            break
        try:
            await asyncio.wait_for(marker_poll.wait(), timeout=0.2)
        except TimeoutError:
            pass
    if not marker_exists:
        raise RuntimeError(
            "MULTI cancellation marker was not written before timeout."
        )

    cancel_requested = await _mcp_result(
        mcp,
        "execution_cancel",
        {
            "request": {
                "execution_id": execution_id,
                "idempotency_key": f"multi-cancel-command-{unique}",
                "reason": "multi running cancellation regression E2E",
                "actor": actor,
            }
        },
    )
    if cancel_requested["state"]["status"] != "CANCEL_REQUESTED":
        raise RuntimeError(
            f"MCP MULTI cancellation was not accepted: {cancel_requested}"
        )
    cancelled, cancelled_states = await _wait_for_status(
        "MCP",
        execution_id,
        {"CANCELLED"},
        mcp=mcp,
        rest=rest,
        timeout_seconds=timeout_seconds,
    )
    if (
        cancelled["state"]["cancellation_reason"]
        != "multi running cancellation regression E2E"
        or cancelled["runtime"]["session_id"] is not None
        or cancelled["recovery"]["runtime_session_cleanup_status"]
        != "SUCCEEDED"
    ):
        raise RuntimeError(
            f"MULTI cancellation did not clean up safely: {cancelled}"
        )
    if await _runtime_session_exists(
        session_factory, target_id, runtime_session_id
    ):
        raise RuntimeError(
            "Cancelled MULTI Execution leaked its Jupyter session."
        )

    snapshot = await _wait_for_published_snapshot(
        session_factory, UUID(execution_id), timeout_seconds
    )
    step_statuses = tuple(_enum_value(step.status) for step in snapshot.steps)
    if (
        snapshot.execution_status != "CANCELLED"
        or snapshot.runtime_session_id is not None
        or snapshot.cleanup_status != "SUCCEEDED"
        or step_statuses != ("CANCELLED",)
        or tuple(_enum_value(attempt.status) for attempt in snapshot.attempts)
        != ("CANCELLED",)
        or tuple(_enum_value(step.status) for step in snapshot.step_attempts)
        != ("CANCELLED",)
    ):
        raise RuntimeError(
            f"PostgreSQL MULTI cancellation is inconsistent: {snapshot}"
        )
    attempts = await _page(
        "REST", execution_id, "attempts", mcp=mcp, rest=rest
    )
    steps = await _page("MCP", execution_id, "steps", mcp=mcp, rest=rest)
    if [item["state"]["status"] for item in attempts] != ["CANCELLED"] or [
        item["result"]["status"] for item in steps
    ] != ["CANCELLED"]:
        raise RuntimeError(
            "Public MULTI cancellation history differs from PostgreSQL."
        )
    attempt_id = str(attempts[0]["attempt_id"])
    detail = await _attempt_detail(
        "REST", execution_id, attempt_id, mcp=mcp, rest=rest
    )
    history = await _attempt_steps(
        "MCP", execution_id, attempt_id, mcp=mcp, rest=rest
    )
    if (
        detail["runtime"]["session_id"] != runtime_session_id
        or detail["recovery"]["runtime_session_cleanup_status"] != "SUCCEEDED"
        or [item["result"]["status"] for item in history] != ["CANCELLED"]
    ):
        raise RuntimeError("MULTI cancelled Attempt history is inconsistent.")

    cancel_artifacts = [
        artifact
        for artifact in snapshot.artifacts
        if artifact.name == "multi-cancel.txt"
    ]
    if tuple(
        _enum_value(artifact.status) for artifact in cancel_artifacts
    ) != ("INCOMPLETE",):
        raise RuntimeError(
            f"Cancelled MULTI Artifact was not preserved: {cancel_artifacts}"
        )
    if (
        cancel_artifacts[0].checksum_sha256
        != hashlib.sha256(b"started").hexdigest()
    ):
        raise RuntimeError(
            "Cancelled MULTI Artifact contains unexpected data."
        )
    if any(
        _enum_value(artifact.artifact_type) == "NOTEBOOK"
        for artifact in snapshot.artifacts
    ):
        raise RuntimeError(
            "Cancelled MULTI Execution registered a successful notebook."
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
        "execution.submitted",
        "execution.started",
        "execution.cancel_requested",
        "execution.artifact_registered",
        "execution.cancelled",
    }
    if not required_events.issubset(event_types):
        raise RuntimeError(
            f"MULTI cancellation event timeline is incomplete: {event_types}"
        )
    return CaseResult(
        name="MCP running cancel",
        execution_id=execution_id,
        statuses=(*running_states, "CANCEL_REQUESTED", *cancelled_states),
        step_statuses=step_statuses,
        artifact_statuses=tuple(
            _enum_value(row.status) for row in cancel_artifacts
        ),
        event_types=event_types,
    )


async def main() -> None:
    settings = get_settings()
    mcp_url = os.getenv("EXECUTOR_MCP_URL", "http://127.0.0.1:8000/mcp")
    rest_url = os.getenv("EXECUTOR_REST_URL", "http://127.0.0.1:8000/api/v1")
    runtime_profile = os.getenv("MULTI_LIFECYCLE_RUNTIME_PROFILE", "basic")
    timeout_seconds = float(
        os.getenv("MULTI_LIFECYCLE_TIMEOUT_SECONDS", "120")
    )
    scan_limit = int(os.getenv("MULTI_LIFECYCLE_STREAM_SCAN_LIMIT", "10000"))
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
                await _run_correction_and_finalization_case(
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
                await _run_running_cancel_case(
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
            print("observed_statuses:", " -> ".join(result.statuses))
            print("step_statuses:", list(result.step_statuses))
            print("artifact_statuses:", list(result.artifact_statuses))
            print("outbox_and_redis_events:", list(result.event_types))
    finally:
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
