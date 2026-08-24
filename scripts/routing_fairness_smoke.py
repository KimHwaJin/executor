"""Verify unsupported profiles, Runtime capacity, FIFO queueing, and context isolation."""

import asyncio
from datetime import datetime
from typing import Any
from uuid import uuid4

from execution_spec_payload import execution_request, inline_spec
from local_test_support import (
    executor_mcp_url,
    register_local_runtime_targets,
    required_tool_result,
    write_report,
)
from mcp import Client

TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED"}


async def _submit(
    client: Client,
    *,
    run_id: str,
    index: int,
    user_id: str,
    sleep_seconds: float,
) -> str:
    marker = f"ROUTING_FAIRNESS:{run_id}:{index}:{user_id}"
    submitted = await required_tool_result(
        client,
        "execution_submit",
        {
            "request": execution_request(
                idempotency_key=f"routing-fairness-submit-{run_id}-{index}",
                operation_mode="SINGLE",
                trigger_type="INTERACTIVE",
                actor={"type": "USER", "id": user_id},
                runtime_profile="basic",
                spec=inline_spec(
                    [
                        {
                            "skill_name": "evaluation",
                            "tool_name": "routing_fairness",
                            "code": (
                                "import time\n"
                                f"time.sleep({sleep_seconds})\n"
                                f"print({marker!r})"
                            ),
                        }
                    ]
                ),
                context={
                    "user_id": user_id,
                    "project_id": f"routing-project-{user_id}",
                    "session_id": f"routing-session-{run_id}-{index}",
                    "task_id": f"routing-task-{run_id}-{index}",
                },
            )
        },
    )
    return str(submitted["execution_id"])


async def _states(
    client: Client, execution_ids: list[str]
) -> list[dict[str, Any]]:
    return list(
        await asyncio.gather(
            *(
                required_tool_result(
                    client, "execution_get", {"execution_id": execution_id}
                )
                for execution_id in execution_ids
            )
        )
    )


async def _wait_statuses(
    client: Client,
    execution_ids: list[str],
    expected: list[str],
    *,
    attempts: int = 600,
) -> list[dict[str, Any]]:
    for _ in range(attempts):
        states = await _states(client, execution_ids)
        if [state["state"]["status"] for state in states] == expected:
            return states
        await asyncio.sleep(0.1)
    current = await _states(client, execution_ids)
    raise TimeoutError(f"Executions did not reach {expected}: {current}")


async def _wait_terminal(
    client: Client, execution_ids: list[str]
) -> list[dict[str, Any]]:
    for _ in range(1200):
        states = await _states(client, execution_ids)
        if all(
            state["state"]["status"] in TERMINAL_STATUSES for state in states
        ):
            return states
        await asyncio.sleep(0.1)
    raise TimeoutError(
        f"Executions did not finish: {await _states(client, execution_ids)}"
    )


async def _attempt_started_at(client: Client, execution_id: str) -> datetime:
    page = await required_tool_result(
        client,
        "execution_attempt_list",
        {"execution_id": execution_id, "limit": 10},
    )
    if len(page["items"]) != 1:
        raise RuntimeError(f"Expected one Attempt for {execution_id}: {page}")
    started_at = page["items"][0]["lifecycle"]["started_at"]
    if not isinstance(started_at, str):
        raise RuntimeError(
            f"Attempt start timestamp is missing for {execution_id}: {page}"
        )
    return datetime.fromisoformat(started_at.replace("Z", "+00:00"))


async def _assert_unsupported_profile(client: Client, run_id: str) -> str:
    response = await client.call_tool(
        "execution_submit",
        {
            "request": execution_request(
                idempotency_key=f"unsupported-profile-{run_id}",
                operation_mode="SINGLE",
                trigger_type="INTERACTIVE",
                actor={"type": "USER", "id": "routing-user"},
                runtime_profile="not-a-runtime-profile",
                spec=inline_spec([{"code": "print('must not execute')"}]),
                context={
                    "user_id": "routing-user",
                    "task_id": f"unsupported-profile-task-{run_id}",
                },
            )
        },
    )
    text = " ".join(str(block) for block in response.content)
    if not response.is_error or "not supported" not in text:
        raise RuntimeError(
            f"Unsupported Runtime Profile was not rejected: {response}"
        )
    return text


async def _assert_profile_routing(
    client: Client, run_id: str
) -> dict[str, dict[str, str]]:
    results: dict[str, dict[str, str]] = {}
    for index, (profile, expected_version) in enumerate(
        (("basic", "3.11"), ("ml", "3.12")), start=100
    ):
        marker = f"PROFILE_VERSION:{profile}:"
        submitted = await required_tool_result(
            client,
            "execution_submit",
            {
                "request": execution_request(
                    idempotency_key=f"profile-routing-{run_id}-{profile}",
                    operation_mode="SINGLE",
                    trigger_type="INTERACTIVE",
                    actor={"type": "USER", "id": "profile-routing-user"},
                    runtime_profile=profile,
                    spec=inline_spec(
                        [
                            {
                                "skill_name": "evaluation",
                                "tool_name": "profile_routing",
                                "code": (
                                    "import sys\n"
                                    f"print({marker!r} + "
                                    "f'{sys.version_info.major}.{sys.version_info.minor}')"
                                ),
                            }
                        ]
                    ),
                    context={
                        "user_id": "profile-routing-user",
                        "project_id": "profile-routing-project",
                        "session_id": f"profile-routing-session-{run_id}-{index}",
                        "task_id": f"profile-routing-task-{run_id}-{index}",
                    },
                )
            },
        )
        execution_id = str(submitted["execution_id"])
        terminal = (await _wait_terminal(client, [execution_id]))[0]
        if terminal["state"]["status"] != "SUCCEEDED":
            raise RuntimeError(
                f"{profile} profile Execution failed: {terminal}"
            )
        consolidated = await required_tool_result(
            client, "execution_result_get", {"execution_id": execution_id}
        )
        text = "".join(
            output.get("text", "")
            for operation in consolidated["operations"]
            for step in operation["steps"]
            for output in step["result"]["outputs"]
            if output.get("output_type") == "stream"
        )
        if f"{marker}{expected_version}" not in text:
            raise RuntimeError(
                f"{profile} profile used an unexpected Python version: {text!r}"
            )
        if terminal["runtime"]["profile"] != profile:
            raise RuntimeError(
                f"Execution reported the wrong Runtime Profile: {terminal}"
            )
        results[profile] = {
            "execution_id": execution_id,
            "python_version": expected_version,
            "target_id": str(terminal["runtime"]["target_id"]),
        }
    return results


async def main() -> None:
    run_id = uuid4().hex
    async with Client(executor_mcp_url()) as client:
        targets = await register_local_runtime_targets(
            client,
            run_id=run_id,
            include_batch=False,
            include_secondary=True,
        )
        target_ids = {str(target["target_id"]) for target in targets}
        profile_results = await _assert_profile_routing(client, run_id)
        unsupported_error = await _assert_unsupported_profile(client, run_id)

        blocker_ids = [
            await _submit(
                client,
                run_id=run_id,
                index=index,
                user_id=f"routing-user-{index}",
                sleep_seconds=4.0,
            )
            for index in range(2)
        ]
        running = await _wait_statuses(
            client, blocker_ids, ["RUNNING", "RUNNING"]
        )
        blocker_targets = {
            str(state["runtime"]["target_id"]) for state in running
        }
        if blocker_targets != target_ids:
            raise RuntimeError(
                f"Two concurrent Executions were not distributed: {blocker_targets}"
            )

        queued_ids: list[str] = []
        for index in range(2, 5):
            queued_ids.append(
                await _submit(
                    client,
                    run_id=run_id,
                    index=index,
                    user_id=f"routing-user-{index}",
                    sleep_seconds=1.0,
                )
            )
            await asyncio.sleep(0.05)
        await _wait_statuses(client, queued_ids, ["QUEUED", "QUEUED", "QUEUED"])

        peak_active = dict.fromkeys(target_ids, 0)
        all_ids = [*blocker_ids, *queued_ids]
        terminal: list[dict[str, Any]] | None = None
        for _ in range(1200):
            states = await _states(client, all_ids)
            listed = await required_tool_result(
                client, "runtime_target_list", {"limit": 200}
            )
            for target in listed["items"]:
                target_id = str(target["target_id"])
                if target_id not in target_ids:
                    continue
                active = int(target["capacity"]["active_execution_count"])
                peak_active[target_id] = max(peak_active[target_id], active)
                if active > 1:
                    raise RuntimeError(f"Runtime capacity exceeded: {target}")
            if all(
                state["state"]["status"] in TERMINAL_STATUSES
                for state in states
            ):
                terminal = states
                break
            await asyncio.sleep(0.1)
        if terminal is None:
            terminal = await _wait_terminal(client, all_ids)
        failures = [
            state
            for state in terminal
            if state["state"]["status"] != "SUCCEEDED"
        ]
        if failures:
            raise RuntimeError(
                f"Routing fairness Executions failed: {failures}"
            )

        queued_started = [
            await _attempt_started_at(client, execution_id)
            for execution_id in queued_ids
        ]
        fifo_ordered = queued_started == sorted(queued_started)

        contexts: list[dict[str, Any]] = []
        for index, state in enumerate(terminal):
            expected_user = f"routing-user-{index}"
            if state["context"]["user_id"] != expected_user:
                raise RuntimeError(f"Execution context crossed users: {state}")
            workspace = state["workspace"]["path"]
            if (
                not isinstance(workspace, str)
                or f"users/{expected_user}/" not in workspace
            ):
                raise RuntimeError(
                    f"Workspace crossed user boundaries: {state}"
                )
            contexts.append(
                {
                    "execution_id": str(state["execution_id"]),
                    "user_id": expected_user,
                    "session_id": state["context"]["session_id"],
                    "target_id": str(state["runtime"]["target_id"]),
                    "workspace": workspace,
                }
            )

        probes = await asyncio.gather(
            *(
                required_tool_result(
                    client,
                    "runtime_target_probe",
                    {
                        "request": {
                            "target_id": target_id,
                            "actor": {
                                "type": "USER",
                                "id": "routing-operator",
                            },
                        }
                    },
                )
                for target_id in target_ids
            )
        )
        if any(
            target["capacity"]["active_execution_count"] != 0
            or target["capacity"]["active_session_count"] != 0
            for target in probes
        ):
            raise RuntimeError(f"Runtime capacity or sessions leaked: {probes}")

    report = write_report(
        "routing-fairness",
        run_id,
        {
            "status": "PASSED" if fifo_ordered else "FAILED",
            "profile_results": profile_results,
            "unsupported_profile_error": unsupported_error,
            "target_ids": sorted(target_ids),
            "blocker_execution_ids": blocker_ids,
            "queued_execution_ids": queued_ids,
            "queued_attempt_started_at": [
                value.isoformat() for value in queued_started
            ],
            "fifo_ordered": fifo_ordered,
            "peak_active_by_target": peak_active,
            "contexts": contexts,
        },
    )
    print("status:", "PASSED" if fifo_ordered else "FAILED")
    print("profile_results:", profile_results)
    print("unsupported_profile_rejected: True")
    print("distinct_runtime_targets:", len(blocker_targets))
    print("queued_execution_ids:", queued_ids)
    print("fifo_started_at:", [value.isoformat() for value in queued_started])
    print("fifo_ordered:", fifo_ordered)
    print("isolated_context_count:", len(contexts))
    print("report:", report)
    if not fifo_ordered:
        raise RuntimeError(
            "Queued Executions did not start in strict FIFO order."
        )


if __name__ == "__main__":
    asyncio.run(main())
