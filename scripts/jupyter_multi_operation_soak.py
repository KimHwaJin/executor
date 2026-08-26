"""Run long MULTI Operations on one retained Jupyter session and finalize safely."""

import asyncio
import os
from time import monotonic
from typing import Any
from uuid import uuid4

from execution_spec_payload import execution_request
from jupyter_long_running_soak import (
    TERMINAL_STATUSES,
    _attempt_snapshot,
    _multi_step_soak_code,
    _redis_snapshot,
    _wait_for_published_events,
)
from local_test_support import (
    env_float,
    env_int,
    execution_stream_text,
    executor_mcp_url,
    register_local_runtime_targets,
    required_tool_result,
    utc_now_iso,
    write_report,
)
from mcp import Client


def _single_step_spec(
    *,
    sequence: int,
    run_id: str,
    duration_seconds: int,
    output_interval_seconds: int,
    step_timeout_seconds: int,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "steps": [
            {
                "sequence": sequence,
                "payload": {
                    "type": "PYTHON_EXECUTE",
                    "source": {
                        "type": "INLINE",
                        "content": _multi_step_soak_code(
                            run_id,
                            sequence,
                            duration_seconds,
                            output_interval_seconds,
                        ),
                    },
                },
                "step_timeout_seconds": step_timeout_seconds,
                "lineage": {
                    "skill_name": "evaluation",
                    "tool_name": f"long_running_multi_operation_{sequence}",
                    "input_parameters": {
                        "operation_index": sequence,
                        "duration_seconds": duration_seconds,
                    },
                },
            }
        ],
    }


async def _wait_for_status(
    client: Client,
    execution_id: str,
    statuses: set[str],
    *,
    timeout_seconds: int,
    poll_interval_seconds: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    deadline = monotonic() + timeout_seconds
    started = monotonic()
    samples: list[dict[str, Any]] = []
    while monotonic() < deadline:
        execution = await required_tool_result(
            client, "execution_get", {"execution_id": execution_id}
        )
        status = execution["state"]["status"]
        attempt = await _attempt_snapshot(client, execution_id)
        samples.append(
            {
                "observed_at": utc_now_iso(),
                "elapsed_seconds": round(monotonic() - started, 3),
                "status": status,
                "version": execution["state"]["version"],
                "target_id": execution["runtime"]["target_id"],
                "session_id": execution["runtime"]["session_id"],
                "attempt": attempt,
            }
        )
        if status in statuses:
            return execution, samples
        if status in TERMINAL_STATUSES:
            raise RuntimeError(
                f"Execution {execution_id} terminated as {status}; expected {sorted(statuses)}."
            )
        await asyncio.sleep(poll_interval_seconds)
    raise TimeoutError(
        f"Execution {execution_id} did not reach {sorted(statuses)} in time."
    )


async def main() -> None:
    run_id = uuid4().hex
    duration_seconds = env_int("MULTI_SOAK_DURATION_SECONDS", 7200, minimum=10)
    operation_count = env_int("MULTI_SOAK_OPERATION_COUNT", 4, minimum=2)
    output_interval_seconds = env_int(
        "MULTI_SOAK_OUTPUT_INTERVAL_SECONDS", 60, minimum=1
    )
    poll_interval_seconds = env_float(
        "MULTI_SOAK_POLL_INTERVAL_SECONDS", 5.0, minimum=0.2
    )
    timeout_margin_seconds = env_int(
        "MULTI_SOAK_TIMEOUT_MARGIN_SECONDS", 600, minimum=60
    )
    runtime_profile = os.getenv("MULTI_SOAK_RUNTIME_PROFILE", "basic")
    base_duration = duration_seconds // operation_count
    operation_durations = [base_duration] * operation_count
    operation_durations[-1] += duration_seconds - sum(operation_durations)
    if min(operation_durations) < 5:
        raise ValueError(
            "MULTI_SOAK_DURATION_SECONDS must allow at least five seconds per Operation."
        )
    step_timeout_seconds = (
        max(operation_durations) + timeout_margin_seconds // 2
    )
    operation_timeout_seconds = (
        max(operation_durations) + timeout_margin_seconds
    )
    operation_wait_timeout_seconds = max(3600, operation_timeout_seconds)
    actor = {"type": "USER", "id": "multi-soak-user"}
    all_samples: list[dict[str, Any]] = []
    waiting_states: list[dict[str, Any]] = []

    async with Client(executor_mcp_url()) as client:
        targets = await register_local_runtime_targets(
            client,
            run_id=run_id,
            include_batch=False,
            include_secondary=False,
        )
        submitted = await required_tool_result(
            client,
            "execution_submit",
            {
                "request": execution_request(
                    idempotency_key=f"multi-soak-submit-{run_id}",
                    operation_mode="MULTI",
                    operation_wait_timeout_seconds=operation_wait_timeout_seconds,
                    trigger_type="INTERACTIVE",
                    actor=actor,
                    runtime_profile=runtime_profile,
                    operation_timeout_seconds=operation_timeout_seconds,
                    spec=_single_step_spec(
                        sequence=0,
                        run_id=run_id,
                        duration_seconds=operation_durations[0],
                        output_interval_seconds=output_interval_seconds,
                        step_timeout_seconds=step_timeout_seconds,
                    ),
                    context={
                        "user_id": "multi-soak-user",
                        "project_id": "local-resilience",
                        "session_id": f"multi-soak-session-{run_id}",
                        "task_id": f"multi-soak-task-{run_id}",
                    },
                    metadata={
                        "test_type": "LONG_RUNNING_MULTI_OPERATION_SOAK",
                        "test_run_id": run_id,
                        "duration_seconds": duration_seconds,
                        "operation_count": operation_count,
                    },
                )
            },
        )
        execution_id = str(submitted["execution_id"])
        retained_target_id: str | None = None
        retained_session_id: str | None = None

        for operation_index in range(operation_count):
            waiting, samples = await _wait_for_status(
                client,
                execution_id,
                {"WAITING_FOR_OPERATION"},
                timeout_seconds=operation_durations[operation_index]
                + timeout_margin_seconds,
                poll_interval_seconds=poll_interval_seconds,
            )
            all_samples.extend(samples)
            target_id = waiting["runtime"]["target_id"]
            session_id = waiting["runtime"]["session_id"]
            if target_id is None or session_id is None:
                raise RuntimeError(
                    "Waiting MULTI execution did not retain its Runtime session."
                )
            if retained_target_id is None:
                retained_target_id = str(target_id)
                retained_session_id = str(session_id)
            elif (
                str(target_id) != retained_target_id
                or str(session_id) != retained_session_id
            ):
                raise RuntimeError(
                    "MULTI execution changed Runtime Target or session."
                )
            waiting_states.append(
                {
                    "operation_index": operation_index,
                    "observed_at": utc_now_iso(),
                    "version": waiting["state"]["version"],
                    "target_id": target_id,
                    "session_id": session_id,
                }
            )
            if operation_index + 1 < operation_count:
                next_index = operation_index + 1
                created = await required_tool_result(
                    client,
                    "execution_operation_create",
                    {
                        "request": {
                            "execution_id": execution_id,
                            "idempotency_key": (
                                f"multi-soak-operation-{next_index}-{run_id}"
                            ),
                            "expected_version": waiting["state"]["version"],
                            "operation_timeout_seconds": operation_timeout_seconds,
                            "spec": _single_step_spec(
                                sequence=next_index,
                                run_id=run_id,
                                duration_seconds=operation_durations[
                                    next_index
                                ],
                                output_interval_seconds=output_interval_seconds,
                                step_timeout_seconds=step_timeout_seconds,
                            ),
                            "metadata": {"operation_index": next_index},
                            "actor": actor,
                        }
                    },
                )
                if created["state"]["status"] != "QUEUED":
                    raise RuntimeError(
                        f"Follow-up Operation was not queued: {created}"
                    )

        finalization = await required_tool_result(
            client,
            "execution_finalize",
            {
                "request": {
                    "execution_id": execution_id,
                    "idempotency_key": f"multi-soak-finalize-{run_id}",
                    "expected_version": waiting_states[-1]["version"],
                    "actor": actor,
                }
            },
        )
        if finalization["state"]["status"] != "FINALIZING":
            raise RuntimeError(
                f"MULTI finalization was not accepted: {finalization}"
            )
        terminal, final_samples = await _wait_for_status(
            client,
            execution_id,
            {"SUCCEEDED"},
            timeout_seconds=timeout_margin_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        all_samples.extend(final_samples)
        if terminal["runtime"]["session_id"] is not None:
            raise RuntimeError(
                "Finalized MULTI execution retained its Runtime session."
            )

        result = await required_tool_result(
            client, "execution_result_get", {"execution_id": execution_id}
        )
        if len(result["operations"]) != operation_count:
            raise RuntimeError(
                "Consolidated result does not contain every Operation."
            )
        stream_text = await execution_stream_text(client, execution_id)
        for operation_index in range(operation_count):
            for marker in (
                f"SOAK_STEP_START:{run_id}:{operation_index}",
                f"SOAK_STEP_COMPLETE:{run_id}:{operation_index}",
            ):
                if marker not in stream_text:
                    raise RuntimeError(
                        f"MULTI result is missing marker {marker!r}."
                    )
        heartbeat_count = stream_text.count(f"SOAK_STEP_HEARTBEAT:{run_id}")
        expected_heartbeats = sum(
            max(1, item // output_interval_seconds)
            for item in operation_durations
        )
        if heartbeat_count < expected_heartbeats:
            raise RuntimeError(
                f"Expected {expected_heartbeats} heartbeats, found {heartbeat_count}."
            )

        notebook = await required_tool_result(
            client,
            "execution_notebook_read",
            {
                "execution_id": execution_id,
                "view": "FULL",
                "start_index": 0,
                "limit": 200,
            },
        )
        if len(notebook["cells"]) != operation_count:
            raise RuntimeError(
                "MULTI Notebook does not contain one cell per Operation."
            )
        artifact_names = {artifact["name"] for artifact in result["artifacts"]}
        expected_artifacts = {"execution.ipynb"} | {
            f"soak-{run_id}-step-{index}.json"
            for index in range(operation_count)
        }
        if missing_artifacts := expected_artifacts - artifact_names:
            raise RuntimeError(
                f"Missing MULTI Artifacts: {sorted(missing_artifacts)}"
            )

        events = await _wait_for_published_events(client, execution_id)
        event_types = [event["event_type"] for event in events]
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
                f"MULTI event history is incomplete: {event_types}"
            )

        target = await required_tool_result(
            client,
            "runtime_target_probe",
            {"request": {"target_id": retained_target_id, "actor": actor}},
        )
        if target["capacity"]["active_execution_count"] != 0:
            raise RuntimeError(
                f"Runtime reservation leaked: {target['capacity']}"
            )
        if target["capacity"]["active_session_count"] != 0:
            raise RuntimeError(f"Runtime session leaked: {target['capacity']}")

    redis_snapshot = await _redis_snapshot(execution_id)
    if redis_snapshot["matching_event_count"] < len(events):
        raise RuntimeError(
            "Redis event Stream is missing published integration events."
        )
    if redis_snapshot["work_pending_count"]:
        raise RuntimeError(
            f"Redis work messages remain pending: {redis_snapshot}"
        )

    report = write_report(
        "jupyter-multi-operation-soak",
        run_id,
        {
            "status": "PASSED",
            "execution_id": execution_id,
            "configuration": {
                "duration_seconds": duration_seconds,
                "operation_count": operation_count,
                "operation_durations": operation_durations,
                "output_interval_seconds": output_interval_seconds,
                "poll_interval_seconds": poll_interval_seconds,
                "runtime_profile": runtime_profile,
                "step_timeout_seconds": step_timeout_seconds,
                "operation_timeout_seconds": operation_timeout_seconds,
                "operation_wait_timeout_seconds": operation_wait_timeout_seconds,
            },
            "registered_target_ids": [
                str(item["target_id"]) for item in targets
            ],
            "retained_target_id": retained_target_id,
            "retained_session_id": retained_session_id,
            "waiting_states": waiting_states,
            "samples": all_samples,
            "heartbeat_count": heartbeat_count,
            "event_types": event_types,
            "artifact_names": sorted(artifact_names),
            "notebook_path": terminal["workspace"]["notebook_path"],
            "runtime_resources": target["resources"],
            "redis": redis_snapshot,
        },
    )
    print("status: PASSED")
    print("execution_id:", execution_id)
    print("duration_seconds:", duration_seconds)
    print("operation_count:", operation_count)
    print("heartbeat_count:", heartbeat_count)
    print("retained_runtime_session:", retained_session_id)
    print("report:", report)


if __name__ == "__main__":
    asyncio.run(main())
