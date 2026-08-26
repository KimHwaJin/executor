"""Run configurable long Jupyter Steps and verify their durable lifecycle end to end."""

import asyncio
import os
from time import monotonic
from typing import Any
from uuid import uuid4

from execution_spec_payload import execution_request, inline_spec
from local_test_support import (
    env_bool,
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
from redis.asyncio import Redis
from redis.exceptions import ResponseError

TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED"}


def _soak_code(
    run_id: str, duration_seconds: int, output_interval_seconds: int
) -> str:
    return (
        "import json\n"
        "import time\n"
        "from datetime import datetime, timezone\n"
        "from pathlib import Path\n"
        f"run_id = {run_id!r}\n"
        f"duration_seconds = {duration_seconds}\n"
        f"output_interval_seconds = {output_interval_seconds}\n"
        "started = time.monotonic()\n"
        "next_output = started\n"
        "heartbeats = []\n"
        "print(f'SOAK_START:{run_id}', flush=True)\n"
        "while True:\n"
        "    elapsed = time.monotonic() - started\n"
        "    if elapsed >= duration_seconds:\n"
        "        break\n"
        "    if time.monotonic() >= next_output:\n"
        "        timestamp = datetime.now(timezone.utc).isoformat()\n"
        "        heartbeats.append({'elapsed_seconds': round(elapsed, 3), 'at': timestamp})\n"
        "        print(f'SOAK_HEARTBEAT:{run_id}:{elapsed:.3f}:{timestamp}', flush=True)\n"
        "        next_output += output_interval_seconds\n"
        "    time.sleep(min(1.0, max(0.05, duration_seconds - elapsed)))\n"
        "completed_at = datetime.now(timezone.utc).isoformat()\n"
        "Path('artifacts/logs').mkdir(parents=True, exist_ok=True)\n"
        f"artifact = Path('artifacts/logs/soak-{run_id}.json')\n"
        "artifact.write_text(json.dumps({\n"
        "    'run_id': run_id,\n"
        "    'duration_seconds': duration_seconds,\n"
        "    'heartbeats': heartbeats,\n"
        "    'completed_at': completed_at,\n"
        "}, indent=2), encoding='utf-8')\n"
        "print(f'SOAK_COMPLETE:{run_id}:{completed_at}', flush=True)\n"
    )


def _multi_step_soak_code(
    run_id: str,
    step_index: int,
    duration_seconds: int,
    output_interval_seconds: int,
) -> str:
    return (
        "import json\n"
        "import time\n"
        "from datetime import datetime, timezone\n"
        "from pathlib import Path\n"
        f"run_id = {run_id!r}\n"
        f"step_index = {step_index}\n"
        f"duration_seconds = {duration_seconds}\n"
        f"output_interval_seconds = {output_interval_seconds}\n"
        "started = time.monotonic()\n"
        "next_output = started\n"
        "heartbeats = []\n"
        "print(f'SOAK_STEP_START:{run_id}:{step_index}', flush=True)\n"
        "while True:\n"
        "    elapsed = time.monotonic() - started\n"
        "    if elapsed >= duration_seconds:\n"
        "        break\n"
        "    if time.monotonic() >= next_output:\n"
        "        timestamp = datetime.now(timezone.utc).isoformat()\n"
        "        heartbeats.append({'elapsed_seconds': round(elapsed, 3), 'at': timestamp})\n"
        "        print(\n"
        "            f'SOAK_STEP_HEARTBEAT:{run_id}:{step_index}:{elapsed:.3f}:{timestamp}',\n"
        "            flush=True,\n"
        "        )\n"
        "        next_output += output_interval_seconds\n"
        "    time.sleep(min(1.0, max(0.05, duration_seconds - elapsed)))\n"
        "completed_at = datetime.now(timezone.utc).isoformat()\n"
        "Path('artifacts/logs').mkdir(parents=True, exist_ok=True)\n"
        f"artifact = Path('artifacts/logs/soak-{run_id}-step-{step_index}.json')\n"
        "artifact.write_text(json.dumps({\n"
        "    'run_id': run_id,\n"
        "    'step_index': step_index,\n"
        "    'duration_seconds': duration_seconds,\n"
        "    'heartbeats': heartbeats,\n"
        "    'completed_at': completed_at,\n"
        "}, indent=2), encoding='utf-8')\n"
        "print(f'SOAK_STEP_COMPLETE:{run_id}:{step_index}:{completed_at}', flush=True)\n"
    )


async def _attempt_snapshot(
    client: Client, execution_id: str
) -> dict[str, Any] | None:
    page = await required_tool_result(
        client,
        "execution_attempt_list",
        {"execution_id": execution_id, "limit": 10},
    )
    if not page["items"]:
        return None
    attempt_id = page["items"][-1]["attempt_id"]
    detail = await required_tool_result(
        client,
        "execution_attempt_get",
        {"execution_id": execution_id, "attempt_id": attempt_id},
    )
    return {
        "attempt_id": detail["attempt_id"],
        "status": detail["state"]["status"],
        "target_id": detail["runtime"]["target_id"],
        "session_id": detail["runtime"]["session_id"],
        "lease_owner": detail["lease"]["owner"],
        "lease_expires_at": detail["lease"]["expires_at"],
        "heartbeat_at": detail["lease"]["heartbeat_at"],
    }


async def _wait_for_terminal(
    client: Client,
    execution_id: str,
    *,
    duration_seconds: int,
    poll_interval_seconds: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    samples: list[dict[str, Any]] = []
    started = monotonic()
    deadline = started + duration_seconds + max(900, duration_seconds // 10)
    last_status: str | None = None
    while monotonic() < deadline:
        execution = await required_tool_result(
            client, "execution_get", {"execution_id": execution_id}
        )
        status = execution["state"]["status"]
        attempt = await _attempt_snapshot(client, execution_id)
        sample = {
            "observed_at": utc_now_iso(),
            "elapsed_seconds": round(monotonic() - started, 3),
            "status": status,
            "version": execution["state"]["version"],
            "target_id": execution["runtime"]["target_id"],
            "session_id": execution["runtime"]["session_id"],
            "attempt": attempt,
        }
        if status != last_status or status == "RUNNING":
            samples.append(sample)
        last_status = status
        if status in TERMINAL_STATUSES:
            return execution, samples
        await asyncio.sleep(poll_interval_seconds)
    raise TimeoutError(
        f"Execution {execution_id} did not finish within the soak deadline."
    )


async def _wait_for_published_events(
    client: Client,
    execution_id: str,
    *,
    attempts: int = 100,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for _ in range(attempts):
        page = await required_tool_result(
            client,
            "execution_event_list",
            {"execution_id": execution_id, "limit": 500},
        )
        events = page["items"]
        if events and all(
            event["delivery"]["status"] == "PUBLISHED" for event in events
        ):
            return events
        await asyncio.sleep(0.2)
    raise RuntimeError(
        f"Execution {execution_id} still has unpublished Outbox events: {events}"
    )


async def _redis_snapshot(execution_id: str) -> dict[str, Any]:
    redis_url = os.getenv(
        "LOCAL_TEST_REDIS_URL",
        os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
    )
    work_stream = os.getenv("REDIS_WORK_STREAM", "executor.work")
    event_stream = os.getenv("REDIS_EVENT_STREAM", "executor.events")
    work_group = os.getenv("EXECUTION_CONSUMER_GROUP", "executor-workers")
    redis = Redis.from_url(redis_url, decode_responses=True)
    try:
        entries = await redis.xrange(event_stream)
        matching_events = sum(
            1
            for _, fields in entries
            if fields.get("execution_id") == execution_id
        )
        try:
            pending = await redis.xpending(work_stream, work_group)
            pending_count = int(pending.get("pending", 0))
        except ResponseError:
            pending_count = 0
        return {
            "work_stream": work_stream,
            "event_stream": event_stream,
            "matching_event_count": matching_events,
            "work_pending_count": pending_count,
        }
    finally:
        await redis.aclose()


async def main() -> None:
    run_id = uuid4().hex
    duration_seconds = env_int("SOAK_DURATION_SECONDS", 300, minimum=5)
    step_count = env_int("SOAK_STEP_COUNT", 1, minimum=1)
    output_interval_seconds = env_int(
        "SOAK_OUTPUT_INTERVAL_SECONDS", 60, minimum=1
    )
    poll_interval_seconds = env_float(
        "SOAK_POLL_INTERVAL_SECONDS", 5.0, minimum=0.2
    )
    profile = os.getenv("SOAK_RUNTIME_PROFILE", "basic")
    timeout_margin_seconds = env_int(
        "SOAK_TIMEOUT_MARGIN_SECONDS", 600, minimum=60
    )
    base_step_duration = duration_seconds // step_count
    step_durations = [base_step_duration] * step_count
    step_durations[-1] += duration_seconds - sum(step_durations)
    if min(step_durations) < 5:
        raise ValueError(
            "SOAK_DURATION_SECONDS must allow at least five seconds per Step."
        )
    step_timeout_seconds = max(step_durations) + timeout_margin_seconds // 2
    operation_timeout_seconds = duration_seconds + timeout_margin_seconds

    if step_count == 1:
        steps = [
            {
                "skill_name": "evaluation",
                "tool_name": "long_running_soak",
                "code": _soak_code(
                    run_id, duration_seconds, output_interval_seconds
                ),
                "step_timeout_seconds": step_timeout_seconds,
            }
        ]
    else:
        steps = [
            {
                "skill_name": "evaluation",
                "tool_name": f"long_running_soak_step_{step_index}",
                "code": _multi_step_soak_code(
                    run_id,
                    step_index,
                    step_duration,
                    output_interval_seconds,
                ),
                "step_timeout_seconds": step_timeout_seconds,
            }
            for step_index, step_duration in enumerate(step_durations)
        ]

    async with Client(executor_mcp_url()) as client:
        targets = await register_local_runtime_targets(
            client,
            run_id=run_id,
            include_batch=False,
            include_secondary=env_bool("SOAK_INCLUDE_SECONDARY_TARGET", False),
        )
        submitted = await required_tool_result(
            client,
            "execution_submit",
            {
                "request": execution_request(
                    idempotency_key=f"long-soak-submit-{run_id}",
                    operation_mode="SINGLE",
                    trigger_type="INTERACTIVE",
                    actor={"type": "USER", "id": "long-soak-user"},
                    runtime_profile=profile,
                    operation_timeout_seconds=operation_timeout_seconds,
                    spec=inline_spec(steps),
                    context={
                        "user_id": "long-soak-user",
                        "project_id": "local-resilience",
                        "session_id": f"long-soak-session-{run_id}",
                        "task_id": f"long-soak-task-{run_id}",
                    },
                    metadata={
                        "test_type": "LONG_RUNNING_SOAK",
                        "test_run_id": run_id,
                        "duration_seconds": duration_seconds,
                        "step_count": step_count,
                    },
                )
            },
        )
        execution_id = str(submitted["execution_id"])
        terminal, samples = await _wait_for_terminal(
            client,
            execution_id,
            duration_seconds=duration_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        if terminal["state"]["status"] != "SUCCEEDED":
            raise RuntimeError(
                f"Long-running soak did not succeed: {terminal}"
            )

        result = await required_tool_result(
            client, "execution_result_get", {"execution_id": execution_id}
        )
        stream_text = await execution_stream_text(client, execution_id)
        if step_count == 1:
            markers = (f"SOAK_START:{run_id}", f"SOAK_COMPLETE:{run_id}")
            heartbeat_marker = f"SOAK_HEARTBEAT:{run_id}"
        else:
            markers = tuple(
                marker
                for step_index in range(step_count)
                for marker in (
                    f"SOAK_STEP_START:{run_id}:{step_index}",
                    f"SOAK_STEP_COMPLETE:{run_id}:{step_index}",
                )
            )
            heartbeat_marker = f"SOAK_STEP_HEARTBEAT:{run_id}"
        for marker in markers:
            if marker not in stream_text:
                raise RuntimeError(
                    f"Long-running output is missing marker {marker!r}."
                )
        heartbeat_count = stream_text.count(heartbeat_marker)
        expected_minimum = sum(
            max(1, step_duration // output_interval_seconds)
            for step_duration in step_durations
        )
        if heartbeat_count < expected_minimum:
            raise RuntimeError(
                f"Expected at least {expected_minimum} application heartbeats, "
                f"found {heartbeat_count}."
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
        if len(notebook["cells"]) < step_count:
            raise RuntimeError(
                f"Runtime-owned Notebook has {len(notebook['cells'])} cells; "
                f"expected at least {step_count}."
            )
        for step_index, notebook_cell in enumerate(
            notebook["cells"][:step_count]
        ):
            if f"run_id = {run_id!r}" not in notebook_cell["source"]:
                raise RuntimeError(
                    f"Runtime-owned Notebook cell {step_index} does not match the soak run."
                )
            expected_outputs = (
                max(1, step_durations[step_index] // output_interval_seconds)
                + 2
            )
            if (
                notebook_cell["output_summary"]["output_count"]
                < expected_outputs
            ):
                raise RuntimeError(
                    f"Runtime-owned Notebook cell {step_index} output summary is incomplete."
                )
        artifacts = result["artifacts"]
        artifact_names = {artifact["name"] for artifact in artifacts}
        expected_artifacts = (
            {f"soak-{run_id}.json"}
            if step_count == 1
            else {
                f"soak-{run_id}-step-{step_index}.json"
                for step_index in range(step_count)
            }
        )
        missing_artifacts = (
            expected_artifacts | {"execution.ipynb"}
        ) - artifact_names
        if missing_artifacts:
            raise RuntimeError(
                f"Expected soak Artifacts are missing: {sorted(artifact_names)}"
            )

        events = await _wait_for_published_events(client, execution_id)
        terminal_events = [
            event
            for event in events
            if event["event_type"] == "execution.completed"
            and event["payload"]["status"] == "SUCCEEDED"
        ]
        if len(terminal_events) != 1:
            raise RuntimeError(
                "Expected exactly one successful execution.completed event, "
                f"found {len(terminal_events)}."
            )

        target_id = str(terminal["runtime"]["target_id"])
        target = await required_tool_result(
            client,
            "runtime_target_probe",
            {
                "request": {
                    "target_id": target_id,
                    "actor": {"type": "USER", "id": "long-soak-user"},
                }
            },
        )
        if target["capacity"]["active_execution_count"] != 0:
            raise RuntimeError(
                f"Runtime reservation leaked after soak: {target['capacity']}"
            )
        if target["capacity"]["active_session_count"] != 0:
            raise RuntimeError(
                f"Jupyter kernel leaked after soak: {target['capacity']}"
            )

    redis_snapshot = await _redis_snapshot(execution_id)
    if redis_snapshot["matching_event_count"] < len(events):
        raise RuntimeError(
            "Redis event count is smaller than the published integration Outbox count: "
            f"{redis_snapshot} vs {len(events)}"
        )
    if (
        env_bool("SOAK_REQUIRE_EMPTY_WORK_PENDING", True)
        and redis_snapshot["work_pending_count"]
    ):
        raise RuntimeError(
            f"Redis work Stream still has pending messages: {redis_snapshot}"
        )

    report = write_report(
        "jupyter-long-running-soak",
        run_id,
        {
            "status": "PASSED",
            "execution_id": execution_id,
            "configuration": {
                "duration_seconds": duration_seconds,
                "step_count": step_count,
                "step_durations": step_durations,
                "output_interval_seconds": output_interval_seconds,
                "poll_interval_seconds": poll_interval_seconds,
                "runtime_profile": profile,
                "step_timeout_seconds": step_timeout_seconds,
                "operation_timeout_seconds": operation_timeout_seconds,
            },
            "registered_target_ids": [
                str(target["target_id"]) for target in targets
            ],
            "selected_target_id": target_id,
            "heartbeat_count": heartbeat_count,
            "samples": samples,
            "event_types": [event["event_type"] for event in events],
            "artifact_names": sorted(artifact_names),
            "notebook_path": terminal["workspace"]["notebook_path"],
            "runtime_resources": target["resources"],
            "redis": redis_snapshot,
        },
    )
    print("status: PASSED")
    print("execution_id:", execution_id)
    print("duration_seconds:", duration_seconds)
    print("step_count:", step_count)
    print("heartbeat_count:", heartbeat_count)
    print("report:", report)


if __name__ == "__main__":
    asyncio.run(main())
