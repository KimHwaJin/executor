"""Run configurable Executor load waves and report latency, capacity, and leak evidence."""

import argparse
import asyncio
import statistics
from datetime import datetime
from time import monotonic
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


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--sleep-seconds", type=float, default=0.5)
    parser.add_argument(
        "--pattern", choices=("uniform", "long-short"), default="uniform"
    )
    parser.add_argument(
        "--profiles", choices=("default", "mixed"), default="mixed"
    )
    parser.add_argument(
        "--pool", choices=("INTERACTIVE", "BATCH"), default="INTERACTIVE"
    )
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    args = parser.parse_args()
    if not 1 <= args.count <= 100:
        parser.error("--count must be between 1 and 100")
    if not 1 <= args.rounds <= 10:
        parser.error("--rounds must be between 1 and 10")
    if args.sleep_seconds < 0:
        parser.error("--sleep-seconds must not be negative")
    if args.timeout_seconds < 30:
        parser.error("--timeout-seconds must be at least 30")
    return args


def _parse_time(value: str | None) -> datetime | None:
    return (
        datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    )


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * percentile))
    return round(ordered[index], 6)


def _sleep_for(args: argparse.Namespace, index: int) -> float:
    if args.pattern == "long-short":
        return 3.0 if index % 5 == 0 else 0.1
    return args.sleep_seconds


async def _submit(
    client: Client,
    *,
    args: argparse.Namespace,
    run_id: str,
    round_number: int,
    index: int,
    project_id: str,
) -> str:
    profile = (
        "3102311"
        if args.profiles == "mixed" and index % 2
        else "default"
    )
    sleep_seconds = _sleep_for(args, index)
    user_id = f"load-user-{index % 10}"
    marker = (
        f"LOAD_WAVE:{run_id}:{round_number}:{index}:{profile}:{sleep_seconds}"
    )
    submitted = await required_tool_result(
        client,
        "execution_submit",
        {
            "request": execution_request(
                idempotency_key=f"load-wave-{run_id}-{round_number}-{index}",
                operation_mode="SINGLE",
                trigger_type=args.pool,
                actor={
                    "type": "BATCH" if args.pool == "BATCH" else "USER",
                    "id": (
                        f"load-wave-{args.pool.lower()}"
                        if args.pool == "BATCH"
                        else user_id
                    ),
                },
                runtime_profile=profile,
                operation_timeout_seconds=max(60, int(sleep_seconds) + 30),
                spec=inline_spec(
                    [
                        {
                            "skill_name": "evaluation",
                            "tool_name": f"load_wave_{args.pattern.replace('-', '_')}",
                            "code": (
                                "import time\n"
                                f"time.sleep({sleep_seconds})\n"
                                f"print({marker!r})"
                            ),
                            "step_timeout_seconds": max(
                                30, int(sleep_seconds) + 15
                            ),
                        }
                    ]
                ),
                context={
                    "user_id": user_id,
                    "project_id": project_id,
                    "session_id": f"load-session-{run_id}-{round_number}-{index}",
                    "task_id": f"load-task-{run_id}-{round_number}-{index}",
                    "workflow_id": (
                        f"load-workflow-{run_id}-{round_number}-{index}"
                        if args.pool == "BATCH"
                        else None
                    ),
                },
                metadata={
                    "test_type": "CONCURRENT_LOAD_WAVE",
                    "test_run_id": run_id,
                    "round": round_number,
                    "index": index,
                },
            )
        },
    )
    return str(submitted["execution_id"])


async def _project_executions(
    client: Client, project_id: str
) -> list[dict[str, Any]]:
    page = await required_tool_result(
        client,
        "execution_list",
        {"project_id": project_id, "limit": 200},
    )
    if page["has_more"] or page["next_cursor"] is not None:
        raise RuntimeError(
            f"Load wave unexpectedly required pagination: {page}"
        )
    return page["items"]


async def _target_capacity(
    client: Client, target_ids: set[str]
) -> dict[str, tuple[int, int]]:
    page = await required_tool_result(
        client, "runtime_target_list", {"limit": 200}
    )
    return {
        str(item["target_id"]): (
            int(item["capacity"]["active_execution_count"]),
            int(item["capacity"]["max_concurrent_executions"]),
        )
        for item in page["items"]
        if str(item["target_id"]) in target_ids
    }


async def _run_round(
    client: Client,
    *,
    args: argparse.Namespace,
    run_id: str,
    round_number: int,
    target_ids: set[str],
) -> dict[str, Any]:
    project_id = f"load-{args.name}-{run_id}-{round_number}"
    started = monotonic()
    execution_ids = []
    for index in range(args.count):
        execution_ids.append(
            await _submit(
                client,
                args=args,
                run_id=run_id,
                round_number=round_number,
                index=index,
                project_id=project_id,
            )
        )
    submission_seconds = monotonic() - started
    expected_ids = set(execution_ids)
    peak_active = dict.fromkeys(target_ids, 0)
    queued_observed = False
    deadline = monotonic() + args.timeout_seconds
    final_items: list[dict[str, Any]] = []
    while monotonic() < deadline:
        items = await _project_executions(client, project_id)
        items = [
            item for item in items if str(item["execution_id"]) in expected_ids
        ]
        statuses = [item["state"]["status"] for item in items]
        queued_observed = queued_observed or "QUEUED" in statuses
        capacities = await _target_capacity(client, target_ids)
        for target_id, (active, limit) in capacities.items():
            peak_active[target_id] = max(peak_active[target_id], active)
            if active > limit:
                raise RuntimeError(
                    f"Runtime Target {target_id} exceeded capacity: {active} > {limit}"
                )
        if len(items) == args.count and all(
            status in TERMINAL_STATUSES for status in statuses
        ):
            final_items = items
            break
        await asyncio.sleep(0.2)
    if not final_items:
        raise TimeoutError(
            f"Load wave {args.name} round {round_number} did not finish."
        )
    failures = [
        item for item in final_items if item["state"]["status"] != "SUCCEEDED"
    ]
    if failures:
        raise RuntimeError(f"Load wave contains failed Executions: {failures}")
    if args.count > len(target_ids) and not queued_observed:
        raise RuntimeError(
            "Load wave exceeded capacity but never observed QUEUED work."
        )

    queue_waits: list[float] = []
    total_times: list[float] = []
    for item in final_items:
        created = _parse_time(item["created_at"])
        execution_started = _parse_time(item["lifecycle"]["started_at"])
        finished = _parse_time(item["lifecycle"]["finished_at"])
        if created is None or execution_started is None or finished is None:
            raise RuntimeError(f"Load lifecycle timestamp is missing: {item}")
        queue_waits.append((execution_started - created).total_seconds())
        total_times.append((finished - created).total_seconds())

    probes = await asyncio.gather(
        *(
            required_tool_result(
                client,
                "runtime_target_probe",
                {
                    "request": {
                        "target_id": target_id,
                        "actor": {"type": "USER", "id": "load-test-operator"},
                    }
                },
            )
            for target_id in target_ids
        )
    )
    leaked = [
        target
        for target in probes
        if target["capacity"]["active_execution_count"] != 0
        or target["capacity"]["active_session_count"] != 0
    ]
    if leaked:
        raise RuntimeError(
            f"Load wave leaked Runtime capacity or Sessions: {leaked}"
        )

    return {
        "round": round_number,
        "execution_count": args.count,
        "submission_seconds": round(submission_seconds, 6),
        "wall_seconds": round(monotonic() - started, 6),
        "queued_observed": queued_observed,
        "queue_p50_seconds": _percentile(queue_waits, 0.50),
        "queue_p95_seconds": _percentile(queue_waits, 0.95),
        "total_p50_seconds": _percentile(total_times, 0.50),
        "total_p95_seconds": _percentile(total_times, 0.95),
        "peak_active_by_target": peak_active,
        "execution_ids": execution_ids,
    }


def _assert_no_degradation(
    rounds: list[dict[str, Any]],
) -> dict[str, float] | None:
    if len(rounds) < 6:
        return None
    width = min(3, len(rounds) // 2)
    first_wall = statistics.mean(
        float(item["wall_seconds"]) for item in rounds[:width]
    )
    last_wall = statistics.mean(
        float(item["wall_seconds"]) for item in rounds[-width:]
    )
    ratio = last_wall / first_wall if first_wall else 1.0
    if ratio > 2.0:
        raise RuntimeError(
            f"Repeated load degraded beyond 2x: first={first_wall}, last={last_wall}"
        )
    return {
        "first_rounds_mean_wall_seconds": round(first_wall, 6),
        "last_rounds_mean_wall_seconds": round(last_wall, 6),
        "last_to_first_ratio": round(ratio, 6),
    }


async def main() -> None:
    args = _parse_args()
    run_id = uuid4().hex
    async with Client(executor_mcp_url()) as client:
        registered = await register_local_runtime_targets(
            client,
            run_id=run_id,
            include_batch=args.pool == "BATCH",
            include_secondary=True,
        )
        target_ids = {
            str(target["target_id"])
            for target in registered
            if target["runtime"]["pool"] == args.pool
        }
        if len(target_ids) < 2:
            raise RuntimeError(
                f"Load test requires two {args.pool} Runtime Targets."
            )
        rounds = []
        for round_number in range(1, args.rounds + 1):
            result = await _run_round(
                client,
                args=args,
                run_id=run_id,
                round_number=round_number,
                target_ids=target_ids,
            )
            rounds.append(result)
            print(
                f"round={round_number} wall={result['wall_seconds']} "
                f"queue_p95={result['queue_p95_seconds']} "
                f"total_p95={result['total_p95_seconds']}"
            )
    degradation = _assert_no_degradation(rounds)
    report = write_report(
        f"concurrent-load-{args.name}",
        run_id,
        {
            "status": "PASSED",
            "configuration": {
                "name": args.name,
                "count": args.count,
                "rounds": args.rounds,
                "sleep_seconds": args.sleep_seconds,
                "pattern": args.pattern,
                "profiles": args.profiles,
                "pool": args.pool,
            },
            "target_ids": sorted(target_ids),
            "round_results": rounds,
            "degradation": degradation,
        },
    )
    print("status: PASSED")
    print("total_executions:", args.count * args.rounds)
    print("report:", report)


if __name__ == "__main__":
    asyncio.run(main())
