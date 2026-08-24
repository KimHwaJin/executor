"""Submit mixed Jupyter outputs concurrently and validate consolidated Agent results."""

import asyncio
from dataclasses import dataclass
from time import monotonic
from typing import Any
from uuid import uuid4

from execution_spec_payload import execution_request, inline_spec
from local_test_support import (
    env_float,
    env_int,
    execution_output_content,
    execution_output_items,
    execution_stream_text,
    executor_mcp_url,
    register_local_runtime_targets,
    required_tool_result,
    write_report,
)
from mcp import Client

TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED"}
WORKLOAD_TYPES = (
    "TEXT",
    "TABLE",
    "IMAGE",
    "JSON",
    "ARTIFACT",
    "CPU",
    "MEMORY",
)


@dataclass(slots=True)
class ExecutionTiming:
    execution_id: str
    workload_type: str
    profile: str
    submitted_at: float
    submit_latency_seconds: float
    first_running_at: float | None = None
    finished_at: float | None = None

    def report(self, origin: float) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "workload_type": self.workload_type,
            "profile": self.profile,
            "submitted_at_seconds": round(self.submitted_at - origin, 6),
            "submit_latency_seconds": round(self.submit_latency_seconds, 6),
            "queue_wait_seconds": (
                round(self.first_running_at - self.submitted_at, 6)
                if self.first_running_at is not None
                else None
            ),
            "total_seconds": (
                round(self.finished_at - self.submitted_at, 6)
                if self.finished_at is not None
                else None
            ),
        }


def _workload_code(workload_type: str, run_id: str, index: int) -> str:
    marker = f"MIXED_{workload_type}:{run_id}:{index}"
    if workload_type == "TEXT":
        return f"print({marker!r} + ':' + ('x' * 32768))"
    if workload_type == "TABLE":
        return (
            "import pandas as pd\n"
            "from IPython.display import display\n"
            "frame = pd.DataFrame({'value': range(20), 'square': [i * i for i in range(20)]})\n"
            "display(frame)\n"
            f"print({marker!r})"
        )
    if workload_type == "IMAGE":
        return (
            "import matplotlib.pyplot as plt\n"
            "from IPython.display import display\n"
            "figure, axis = plt.subplots(figsize=(4, 3))\n"
            "axis.plot([0, 1, 2, 3], [0, 1, 4, 9])\n"
            f"axis.set_title({marker!r})\n"
            "display(figure)\n"
            "plt.close(figure)\n"
            f"print({marker!r})"
        )
    if workload_type == "JSON":
        return (
            "from IPython.display import JSON, display\n"
            f"payload = {{'marker': {marker!r}, 'values': list(range(25))}}\n"
            "display(JSON(payload))\n"
            f"print({marker!r})"
        )
    if workload_type == "ARTIFACT":
        filename = f"mixed-{run_id}-{index}.txt"
        return (
            "from pathlib import Path\n"
            "Path('artifacts/other').mkdir(parents=True, exist_ok=True)\n"
            f"artifact = Path('artifacts/other/{filename}')\n"
            f"artifact.write_text({marker!r}, encoding='utf-8')\n"
            f"print({marker!r})"
        )
    if workload_type == "CPU":
        return (
            "checksum = sum(index * index for index in range(1_000_000))\n"
            f"print({marker!r}, checksum)"
        )
    if workload_type == "MEMORY":
        return (
            "buffer = bytearray(16 * 1024 * 1024)\n"
            "buffer[0] = 1\n"
            "size = len(buffer)\n"
            "del buffer\n"
            f"print({marker!r}, size)"
        )
    raise ValueError(f"Unsupported mixed workload type: {workload_type}")


async def _validate_outputs(
    client: Client,
    execution_id: str,
    result: dict[str, Any],
    workload_type: str,
    run_id: str,
    index: int,
) -> dict[str, Any]:
    outputs = await execution_output_items(client, execution_id)
    marker = f"MIXED_{workload_type}:{run_id}:{index}"
    stream_text = await execution_stream_text(client, execution_id)
    if marker not in stream_text:
        raise RuntimeError(
            f"{workload_type} result is missing stream marker {marker!r}."
        )
    mime_types = sorted(
        {
            media_type
            for output in outputs
            for representation in output["representations"]
            for media_type in [representation["media_type"]]
        }
    )
    if workload_type == "TABLE" and "text/html" not in mime_types:
        raise RuntimeError(
            f"TABLE result is missing text/html output: {mime_types}"
        )
    if workload_type == "JSON" and "application/json" not in mime_types:
        raise RuntimeError(
            f"JSON result is missing application/json output: {mime_types}"
        )
    if workload_type == "IMAGE":
        image_representations = [
            (output["output_id"], representation["representation_id"])
            for output in outputs
            for representation in output["representations"]
            if representation["media_type"] == "image/png"
        ]
        if not image_representations:
            raise RuntimeError(
                f"IMAGE result is missing image/png output: {mime_types}"
            )
        output_id, representation_id = image_representations[0]
        image = await execution_output_content(
            client, execution_id, output_id, representation_id
        )
        if not image.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError(
                "IMAGE result does not contain a valid PNG signature."
            )
    artifact_names = sorted(
        artifact["name"] for artifact in result["artifacts"]
    )
    if workload_type == "ARTIFACT":
        expected_name = f"mixed-{run_id}-{index}.txt"
        if expected_name not in artifact_names:
            raise RuntimeError(
                f"ARTIFACT result is missing {expected_name}: {artifact_names}"
            )
    if "execution.ipynb" not in artifact_names:
        raise RuntimeError(
            f"Execution Notebook Artifact is missing: {artifact_names}"
        )
    return {
        "output_count": len(outputs),
        "mime_types": mime_types,
        "artifact_names": artifact_names,
    }


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(
        len(ordered) - 1, max(0, round((len(ordered) - 1) * percentile))
    )
    return round(ordered[index], 6)


async def main() -> None:
    run_id = uuid4().hex
    execution_count = env_int("MIXED_LOAD_EXECUTION_COUNT", 14, minimum=7)
    if execution_count > 30:
        raise ValueError("MIXED_LOAD_EXECUTION_COUNT must not exceed 30.")
    poll_interval_seconds = env_float(
        "MIXED_LOAD_POLL_INTERVAL_SECONDS", 0.2, minimum=0.05
    )
    timeout_seconds = env_float("MIXED_LOAD_TIMEOUT_SECONDS", 600, minimum=30)
    origin = monotonic()
    timings: dict[str, ExecutionTiming] = {}
    queued_observed = False
    peak_active: dict[str, int] = {}

    async with Client(executor_mcp_url()) as client:
        targets = await register_local_runtime_targets(
            client,
            run_id=run_id,
            include_batch=False,
            include_secondary=True,
        )
        target_ids = {str(target["target_id"]) for target in targets}
        capacities = {
            str(target["target_id"]): int(
                target["capacity"]["max_concurrent_executions"]
            )
            for target in targets
        }
        peak_active = dict.fromkeys(target_ids, 0)

        for index in range(execution_count):
            workload_type = WORKLOAD_TYPES[index % len(WORKLOAD_TYPES)]
            profile = "ml" if index % 2 else "basic"
            submitted_at = monotonic()
            submitted = await required_tool_result(
                client,
                "execution_submit",
                {
                    "request": execution_request(
                        idempotency_key=f"mixed-load-submit-{run_id}-{index}",
                        operation_mode="SINGLE",
                        trigger_type="INTERACTIVE",
                        actor={"type": "USER", "id": "mixed-load-user"},
                        runtime_profile=profile,
                        operation_timeout_seconds=180,
                        spec=inline_spec(
                            [
                                {
                                    "skill_name": "evaluation",
                                    "tool_name": f"mixed_{workload_type.lower()}",
                                    "code": _workload_code(
                                        workload_type, run_id, index
                                    ),
                                    "step_timeout_seconds": 120,
                                }
                            ]
                        ),
                        context={
                            "user_id": "mixed-load-user",
                            "project_id": "local-resilience",
                            "session_id": f"mixed-load-session-{run_id}-{index}",
                            "task_id": f"mixed-load-task-{run_id}-{index}",
                        },
                        metadata={
                            "test_type": "MIXED_OUTPUT_LOAD",
                            "test_run_id": run_id,
                            "workload_type": workload_type,
                        },
                    )
                },
            )
            finished_submit = monotonic()
            execution_id = str(submitted["execution_id"])
            timings[execution_id] = ExecutionTiming(
                execution_id=execution_id,
                workload_type=workload_type,
                profile=profile,
                submitted_at=submitted_at,
                submit_latency_seconds=finished_submit - submitted_at,
            )

        terminal_states: dict[str, dict[str, Any]] = {}
        deadline = monotonic() + timeout_seconds
        while monotonic() < deadline:
            states = await asyncio.gather(
                *(
                    required_tool_result(
                        client, "execution_get", {"execution_id": execution_id}
                    )
                    for execution_id in timings
                )
            )
            now = monotonic()
            for execution_id, state in zip(timings, states, strict=True):
                status = state["state"]["status"]
                timing = timings[execution_id]
                queued_observed = queued_observed or status == "QUEUED"
                if status == "RUNNING" and timing.first_running_at is None:
                    timing.first_running_at = now
                if status in TERMINAL_STATUSES:
                    timing.finished_at = timing.finished_at or now
                    terminal_states[execution_id] = state

            listed = await required_tool_result(
                client, "runtime_target_list", {"limit": 200}
            )
            for target in listed["items"]:
                target_id = str(target["target_id"])
                if target_id not in target_ids:
                    continue
                active = int(target["capacity"]["active_execution_count"])
                peak_active[target_id] = max(peak_active[target_id], active)
                if active > capacities[target_id]:
                    raise RuntimeError(
                        f"Runtime capacity exceeded for {target['name']}: "
                        f"active={active}, capacity={capacities[target_id]}"
                    )
            if len(terminal_states) == len(timings):
                break
            await asyncio.sleep(poll_interval_seconds)
        else:
            unfinished = sorted(set(timings) - terminal_states.keys())
            raise TimeoutError(f"Mixed load did not finish: {unfinished}")

        failures = {
            execution_id: state
            for execution_id, state in terminal_states.items()
            if state["state"]["status"] != "SUCCEEDED"
        }
        if failures:
            raise RuntimeError(f"Mixed output executions failed: {failures}")
        if not queued_observed:
            raise RuntimeError(
                "Mixed output load never observed capacity-backed QUEUED work."
            )

        result_summaries: dict[str, dict[str, Any]] = {}
        for index, (execution_id, timing) in enumerate(timings.items()):
            result = await required_tool_result(
                client, "execution_result_get", {"execution_id": execution_id}
            )
            result_summaries[execution_id] = await _validate_outputs(
                client,
                execution_id,
                result,
                timing.workload_type,
                run_id,
                index,
            )

        probes = await asyncio.gather(
            *(
                required_tool_result(
                    client,
                    "runtime_target_probe",
                    {
                        "request": {
                            "target_id": target_id,
                            "actor": {"type": "USER", "id": "mixed-load-user"},
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
                f"Runtime reservation or Jupyter kernel leaked: {leaked}"
            )

    submit_latencies = [
        timing.submit_latency_seconds for timing in timings.values()
    ]
    queue_latencies = [
        timing.first_running_at - timing.submitted_at
        for timing in timings.values()
        if timing.first_running_at is not None
    ]
    total_latencies = [
        timing.finished_at - timing.submitted_at
        for timing in timings.values()
        if timing.finished_at is not None
    ]
    report = write_report(
        "mixed-output-load",
        run_id,
        {
            "status": "PASSED",
            "configuration": {
                "execution_count": execution_count,
                "poll_interval_seconds": poll_interval_seconds,
                "timeout_seconds": timeout_seconds,
            },
            "queued_observed": queued_observed,
            "timings": [timing.report(origin) for timing in timings.values()],
            "latency_seconds": {
                "submit_p50": _percentile(submit_latencies, 0.50),
                "submit_p95": _percentile(submit_latencies, 0.95),
                "submit_p99": _percentile(submit_latencies, 0.99),
                "queue_p50": _percentile(queue_latencies, 0.50),
                "queue_p95": _percentile(queue_latencies, 0.95),
                "total_p50": _percentile(total_latencies, 0.50),
                "total_p95": _percentile(total_latencies, 0.95),
            },
            "peak_active_by_target": peak_active,
            "capacity_by_target": capacities,
            "results": result_summaries,
        },
    )
    print("status: PASSED")
    print("execution_count:", execution_count)
    print("queued_observed:", queued_observed)
    print("submit_p95_seconds:", _percentile(submit_latencies, 0.95))
    print("queue_p95_seconds:", _percentile(queue_latencies, 0.95))
    print("total_p95_seconds:", _percentile(total_latencies, 0.95))
    print("report:", report)


if __name__ == "__main__":
    asyncio.run(main())
