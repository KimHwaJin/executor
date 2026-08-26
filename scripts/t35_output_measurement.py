"""Measure current Executor behavior with large Runtime-owned outputs.

The full matrix intentionally consumes substantial memory, PostgreSQL storage,
and local Runtime capacity. Run the smoke preset first. The full preset requires
an explicit ``--confirm-full`` flag and writes a checkpoint after every wave.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from dataclasses import asdict, dataclass
from time import monotonic
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from execution_spec_payload import execution_request, inline_spec
from local_test_support import (
    executor_http_url,
    write_report,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.config import get_settings
from executor_service.infrastructure.db.session import create_engine

MIB = 1024 * 1024
TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED"}
MEASURED_TABLES = (
    "executions",
    "execution_operations",
    "execution_steps",
    "execution_attempts",
    "execution_step_attempts",
    "execution_artifacts",
    "outbox_events",
)
FULL_TEXT_SIZES_MIB = (1, 5, 10, 25, 50, 100)
FULL_IMAGE_SIZES_MIB = (1, 10, 25, 50)
FULL_CONCURRENCY_LEVELS = (1, 5, 10, 20)


@dataclass(frozen=True, slots=True)
class Scenario:
    output_type: str
    size_mib: int
    concurrency: int

    @property
    def name(self) -> str:
        return (
            f"{self.output_type.lower()}-{self.size_mib}mib-"
            f"concurrency-{self.concurrency}"
        )


@dataclass(frozen=True, slots=True)
class DatabaseSnapshot:
    database_bytes: int
    table_bytes: dict[str, int]
    table_rows: dict[str, int]


@dataclass(frozen=True, slots=True)
class ResourceSample:
    elapsed_seconds: float
    executor_rss_bytes: int | None
    runtime_memory_bytes: int | None
    active_execution_count: int | None
    runtime_target_count: int | None
    errors: tuple[str, ...]


def scenario_matrix(preset: str) -> tuple[Scenario, ...]:
    if preset == "smoke":
        return (
            Scenario("TEXT", 1, 1),
            Scenario("IMAGE", 1, 1),
        )
    if preset != "full":
        raise ValueError(f"Unsupported T35 preset: {preset}")
    return tuple(
        Scenario(output_type, size_mib, concurrency)
        for output_type, sizes in (
            ("TEXT", FULL_TEXT_SIZES_MIB),
            ("IMAGE", FULL_IMAGE_SIZES_MIB),
        )
        for size_mib in sizes
        for concurrency in FULL_CONCURRENCY_LEVELS
    )


def parse_scenario(value: str) -> Scenario:
    try:
        output_type, size, concurrency = value.upper().split(":")
        scenario = Scenario(output_type, int(size), int(concurrency))
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "Scenario must be TYPE:SIZE_MIB:CONCURRENCY."
        ) from exc
    if scenario.output_type not in {"TEXT", "IMAGE"}:
        raise argparse.ArgumentTypeError("TYPE must be TEXT or IMAGE.")
    if scenario.size_mib < 1 or scenario.concurrency < 1:
        raise argparse.ArgumentTypeError(
            "SIZE_MIB and CONCURRENCY must be positive."
        )
    return scenario


def workload_code(
    scenario: Scenario,
    *,
    run_id: str,
    index: int,
) -> str:
    requested_bytes = scenario.size_mib * MIB
    marker = f"T35:{run_id}:{scenario.name}:{index}"
    if scenario.output_type == "TEXT":
        return (
            "import base64\n"
            "import os\n"
            "import sys\n"
            f"requested_bytes = {requested_bytes}\n"
            f"marker = {marker!r}\n"
            "prefix = marker + ':'\n"
            "remaining = requested_bytes - len(prefix)\n"
            "payload = base64.b64encode(os.urandom(((remaining + 3) * 3) // 4))\n"
            "sys.stdout.write(prefix + payload.decode('ascii')[:remaining])\n"
            "sys.stdout.flush()\n"
        )
    return (
        "import base64\n"
        "import os\n"
        "import struct\n"
        "import zlib\n"
        "from IPython.display import display\n"
        f"requested_bytes = {requested_bytes}\n"
        f"marker = {marker!r}\n"
        "def png_chunk(kind, data):\n"
        "    checksum = zlib.crc32(kind + data) & 0xffffffff\n"
        "    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', checksum)\n"
        "signature = b'\\x89PNG\\r\\n\\x1a\\n'\n"
        "ihdr = png_chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))\n"
        "idat = png_chunk(b'IDAT', zlib.compress(b'\\x00\\x00\\x00\\x00'))\n"
        "iend = png_chunk(b'IEND', b'')\n"
        "base = signature + ihdr + idat + iend\n"
        "padding_size = requested_bytes - len(base) - 12\n"
        "if padding_size < len(marker) + 1:\n"
        "    raise ValueError('Requested PNG payload is too small.')\n"
        "padding = marker.encode() + b'\\x00' + os.urandom(padding_size - len(marker) - 1)\n"
        "png = signature + ihdr + idat + png_chunk(b'ruSt', padding) + iend\n"
        "assert len(png) == requested_bytes\n"
        "display({'image/png': base64.b64encode(png).decode('ascii')}, raw=True)\n"
    )


async def _subprocess_output(*command: str) -> str:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        message = stderr.decode(errors="replace").strip()
        raise RuntimeError(
            f"Command {command[0]!r} failed with code "
            f"{process.returncode}: {message}"
        )
    return stdout.decode(errors="replace").strip()


async def resolve_executor_container() -> str:
    configured = os.getenv("T35_EXECUTOR_CONTAINER", "").strip()
    if configured:
        return configured
    container_id = await _subprocess_output(
        "docker", "compose", "ps", "-q", "executor"
    )
    if not container_id:
        raise RuntimeError(
            "Compose Executor container was not found. Set "
            "T35_EXECUTOR_CONTAINER for a non-default container."
        )
    return container_id


async def executor_rss_bytes(container: str) -> int:
    payload = await _subprocess_output(
        "docker",
        "exec",
        container,
        "python",
        "-c",
        (
            "from pathlib import Path; "
            "lines=Path('/proc/1/status').read_text().splitlines(); "
            "print(int(next(line.split()[1] for line in lines "
            "if line.startswith('VmRSS:'))) * 1024)"
        ),
    )
    return int(payload)


async def executor_output_limit_bytes(container: str) -> int:
    payload = await _subprocess_output(
        "docker",
        "exec",
        container,
        "python",
        "-c",
        (
            "from executor_service.config import get_settings; "
            "print(get_settings().runtime_max_output_message_bytes)"
        ),
    )
    return int(payload)


def _database_engine() -> AsyncEngine:
    settings = get_settings()
    database_url = os.getenv("T35_DATABASE_URL", settings.database_dsn)
    return create_engine(
        database_url,
        pool_size=1,
        max_overflow=0,
        pool_timeout_seconds=settings.database_pool_timeout_seconds,
        pool_recycle_seconds=settings.database_pool_recycle_seconds,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )


async def database_snapshot(engine: AsyncEngine) -> DatabaseSnapshot:
    table_bytes: dict[str, int] = {}
    table_rows: dict[str, int] = {}
    async with engine.connect() as connection:
        database_bytes = int(
            await connection.scalar(
                text("SELECT pg_database_size(current_database())")
            )
            or 0
        )
        for table_name in MEASURED_TABLES:
            table_bytes[table_name] = int(
                await connection.scalar(
                    text(
                        "SELECT COALESCE(pg_total_relation_size("
                        "to_regclass(:table_name)), 0)"
                    ),
                    {"table_name": table_name},
                )
                or 0
            )
            table_rows[table_name] = int(
                await connection.scalar(
                    text(f"SELECT count(*) FROM {table_name}")
                )
                or 0
            )
    return DatabaseSnapshot(database_bytes, table_bytes, table_rows)


def database_delta(
    before: DatabaseSnapshot, after: DatabaseSnapshot
) -> dict[str, Any]:
    return {
        "database_bytes": after.database_bytes - before.database_bytes,
        "table_bytes": {
            name: after.table_bytes[name] - before.table_bytes[name]
            for name in MEASURED_TABLES
        },
        "table_rows": {
            name: after.table_rows[name] - before.table_rows[name]
            for name in MEASURED_TABLES
        },
    }


async def _resource_sample(
    client: httpx.AsyncClient,
    *,
    origin: float,
    container: str | None,
    target_ids: set[str],
) -> ResourceSample:
    errors: list[str] = []
    rss_request = (
        executor_rss_bytes(container)
        if container is not None
        else _missing_integer()
    )

    async def worker_request() -> int:
        worker = await client.get("/workerz")
        worker.raise_for_status()
        return int(worker.json()["active_execution_count"])

    async def runtime_request() -> tuple[int | None, int]:
        responses = await asyncio.gather(
            *(
                client.post(
                    f"/api/v1/runtime-targets/{target_id}/probe",
                    json={"actor": {"type": "USER", "id": "t35-monitor"}},
                )
                for target_id in sorted(target_ids)
            )
        )
        for response in responses:
            response.raise_for_status()
        targets = [response.json() for response in responses]
        values = [
            int(item["resources"]["memory"]["used_bytes"])
            for item in targets
            if item["resources"]["memory"]["used_bytes"] is not None
        ]
        target_count = len(targets)
        return (sum(values) if values else None), target_count

    rss_result, active_result, runtime_result = await asyncio.gather(
        rss_request,
        worker_request(),
        runtime_request(),
        return_exceptions=True,
    )
    rss: int | None = None
    active: int | None = None
    runtime_memory: int | None = None
    target_count: int | None = None
    if isinstance(rss_result, BaseException):
        errors.append(f"executor_rss:{type(rss_result).__name__}")
    else:
        rss = rss_result
    if isinstance(active_result, BaseException):
        errors.append(f"worker:{type(active_result).__name__}")
    else:
        active = active_result
    if isinstance(runtime_result, BaseException):
        errors.append(f"runtime_memory:{type(runtime_result).__name__}")
    else:
        runtime_memory, target_count = runtime_result
    return ResourceSample(
        elapsed_seconds=round(monotonic() - origin, 6),
        executor_rss_bytes=rss,
        runtime_memory_bytes=runtime_memory,
        active_execution_count=active,
        runtime_target_count=target_count,
        errors=tuple(errors),
    )


async def _missing_integer() -> int | None:
    return None


async def _sample_resources(
    client: httpx.AsyncClient,
    *,
    origin: float,
    container: str | None,
    target_ids: set[str],
    interval_seconds: float,
    stop: asyncio.Event,
    samples: list[ResourceSample],
) -> None:
    while True:
        samples.append(
            await _resource_sample(
                client,
                origin=origin,
                container=container,
                target_ids=target_ids,
            )
        )
        if stop.is_set():
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass


async def _submit_execution(
    client: httpx.AsyncClient,
    scenario: Scenario,
    run_id: str,
    index: int,
    operation_mode: str,
) -> str:
    payload = execution_request(
        idempotency_key=f"t35-{run_id}-{scenario.name}-{index}",
        operation_mode=operation_mode,
        trigger_type="INTERACTIVE",
        actor={"type": "USER", "id": "t35-user"},
        runtime_profile="basic",
        operation_wait_timeout_seconds=(
            300 if operation_mode == "MULTI" else None
        ),
        operation_timeout_seconds=1800,
        spec=inline_spec(
            [
                {
                    "skill_name": "evaluation",
                    "tool_name": f"t35_{scenario.output_type.lower()}",
                    "code": workload_code(
                        scenario, run_id=run_id, index=index
                    ),
                    "step_timeout_seconds": 1200,
                }
            ]
        ),
        context={
            "user_id": "t35-user",
            "project_id": "t35-output-measurement",
            "session_id": f"t35-{run_id}-{scenario.name}-{index}",
            "task_id": f"t35-{run_id}-{scenario.name}-{index}",
        },
        metadata={
            "test_type": "T35_OUTPUT_MEASUREMENT",
            "test_run_id": run_id,
            "scenario": scenario.name,
        },
    )
    response = await client.post("/api/v1/executions", json=payload)
    if response.is_error:
        raise RuntimeError(
            f"Execution submit returned {response.status_code}: "
            f"{response.text[:2000]}"
        )
    return str(response.json()["execution_id"])


async def _wait_for_terminal(
    client: httpx.AsyncClient,
    execution_ids: list[str],
    *,
    timeout_seconds: float,
    completion_statuses: set[str] = TERMINAL_STATUSES,
) -> tuple[dict[str, dict[str, Any]], float, int]:
    started = monotonic()
    terminal: dict[str, dict[str, Any]] = {}
    peak_running = 0
    while monotonic() - started < timeout_seconds:
        pending = [item for item in execution_ids if item not in terminal]
        responses = await asyncio.gather(
            *(
                client.get(f"/api/v1/executions/{execution_id}")
                for execution_id in pending
            )
        )
        for execution_id, response in zip(pending, responses, strict=True):
            response.raise_for_status()
            state = response.json()
            if state["state"]["status"] in completion_statuses:
                terminal[execution_id] = state
        peak_running = max(
            peak_running,
            sum(
                response.json()["state"]["status"] == "RUNNING"
                for response in responses
            ),
        )
        if len(terminal) == len(execution_ids):
            return terminal, monotonic() - started, peak_running
        await asyncio.sleep(0.2)
    unfinished = sorted(set(execution_ids) - terminal.keys())
    raise TimeoutError(f"T35 Executions did not finish: {unfinished}")


def _notebook_size(result: dict[str, Any]) -> int | None:
    for artifact in result["artifacts"]:
        if artifact["type"] == "NOTEBOOK":
            value = artifact["storage"]["size_bytes"]
            return int(value) if value is not None else None
    return None


async def _retrieve_results(
    client: httpx.AsyncClient, execution_ids: list[str]
) -> list[dict[str, Any]]:
    measurements: list[dict[str, Any]] = []
    for execution_id in execution_ids:
        started = monotonic()
        response = await client.get(
            f"/api/v1/executions/{execution_id}/result",
            timeout=None,
        )
        latency = monotonic() - started
        response.raise_for_status()
        response_size = len(response.content)
        result = response.json()
        step_result_refs = [
            step["result"].get("result_ref")
            for operation in result["operations"]
            for step in operation["steps"]
            if step["result"].get("result_ref") is not None
        ]
        measurements.append(
            {
                "execution_id": execution_id,
                "latency_seconds": round(latency, 6),
                "response_size_bytes": response_size,
                "notebook_size_bytes": _notebook_size(result),
                "step_result_ref_count": len(step_result_refs),
                "incomplete_step_result_ref_count": sum(
                    reference["complete"] is False
                    for reference in step_result_refs
                ),
            }
        )
        del result
        del response
    return measurements


async def _multi_output_limit_failure_type(
    client: httpx.AsyncClient, execution_id: str
) -> str | None:
    response = await client.get(f"/api/v1/executions/{execution_id}")
    response.raise_for_status()
    failure = response.json().get("failure")
    return str(failure["type"]) if isinstance(failure, dict) else None


async def _cleanup_output_limit_executions(
    client: httpx.AsyncClient,
    execution_ids: list[str],
    *,
    run_id: str,
    operation_mode: str,
    timeout_seconds: float,
) -> dict[str, dict[str, str | None]]:
    if operation_mode == "SINGLE":
        for execution_id in execution_ids:
            response = await client.post(
                f"/api/v1/executions/{execution_id}/retry",
                json={
                    "idempotency_key": (
                        f"t35-cleanup-retry-{run_id}-{execution_id}"
                    ),
                    "actor": {"type": "USER", "id": "t35-user"},
                },
            )
            response.raise_for_status()
    for execution_id in execution_ids:
        response = await client.post(
            f"/api/v1/executions/{execution_id}/cancel",
            json={
                "idempotency_key": f"t35-cleanup-{run_id}-{execution_id}",
                "reason": "T35 output-limit cleanup",
                "actor": {"type": "USER", "id": "t35-user"},
            },
        )
        response.raise_for_status()
    cleaned, _, _ = await _wait_for_terminal(
        client,
        execution_ids,
        timeout_seconds=timeout_seconds,
    )
    outcomes: dict[str, dict[str, str | None]] = {
        execution_id: {
            "status": state["state"]["status"],
            "runtime_session_id": state["runtime"]["session_id"],
            "runtime_session_cleanup_status": state["recovery"][
                "runtime_session_cleanup_status"
            ],
        }
        for execution_id, state in cleaned.items()
    }
    if any(
        outcome["status"] != "CANCELLED"
        or outcome["runtime_session_id"] is not None
        or outcome["runtime_session_cleanup_status"] != "SUCCEEDED"
        for outcome in outcomes.values()
    ):
        raise RuntimeError(f"T35 output-limit cleanup failed: {outcomes}")
    return outcomes


def _maximum(samples: list[ResourceSample], field: str) -> int | None:
    values = [
        value
        for sample in samples
        if (value := getattr(sample, field)) is not None
    ]
    return max(values) if values else None


def _first(samples: list[ResourceSample], field: str) -> int | None:
    for sample in samples:
        value = getattr(sample, field)
        if value is not None:
            return value
    return None


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(
        len(ordered) - 1,
        max(0, round((len(ordered) - 1) * percentile)),
    )
    return round(ordered[index], 6)


async def run_scenario(
    scenario: Scenario,
    *,
    run_id: str,
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    container: str | None,
    target_ids: set[str],
    sample_interval_seconds: float,
    timeout_seconds: float,
    expect_output_limit: bool,
    operation_mode: str,
) -> dict[str, Any]:
    database_before = await database_snapshot(engine)
    origin = monotonic()
    samples = [
        await _resource_sample(
            client,
            origin=origin,
            container=container,
            target_ids=target_ids,
        )
    ]
    stop = asyncio.Event()
    sampler = asyncio.create_task(
        _sample_resources(
            client,
            origin=origin,
            container=container,
            target_ids=target_ids,
            interval_seconds=sample_interval_seconds,
            stop=stop,
            samples=samples,
        )
    )
    try:
        submit_started = monotonic()
        execution_ids = list(
            await asyncio.gather(
                *(
                    _submit_execution(
                        client,
                        scenario,
                        run_id,
                        index,
                        operation_mode,
                    )
                    for index in range(scenario.concurrency)
                )
            )
        )
        submit_seconds = monotonic() - submit_started
        expected_limit_status = (
            "WAITING_FOR_OPERATION" if operation_mode == "MULTI" else "FAILED"
        )
        completion_statuses = set(TERMINAL_STATUSES)
        if expect_output_limit and operation_mode == "MULTI":
            completion_statuses.add("WAITING_FOR_OPERATION")
        terminal, execution_seconds, peak_running = await _wait_for_terminal(
            client,
            execution_ids,
            timeout_seconds=timeout_seconds,
            completion_statuses=completion_statuses,
        )
        multi_failure_types: dict[str, str | None] = {}
        if expect_output_limit and operation_mode == "MULTI":
            values = await asyncio.gather(
                *(
                    _multi_output_limit_failure_type(client, execution_id)
                    for execution_id in execution_ids
                )
            )
            multi_failure_types = dict(zip(execution_ids, values, strict=True))
        terminal_outcomes = {}
        for execution_id, state in terminal.items():
            failure = state.get("failure")
            terminal_outcomes[execution_id] = {
                "status": state["state"]["status"],
                "failure_type": (
                    multi_failure_types.get(execution_id)
                    if operation_mode == "MULTI"
                    else failure["type"]
                    if failure is not None
                    else None
                ),
            }
        validation_error: str | None = None
        if expect_output_limit:
            unexpected = {
                execution_id: outcome
                for execution_id, outcome in terminal_outcomes.items()
                if outcome
                != {
                    "status": expected_limit_status,
                    "failure_type": "OUTPUT_LIMIT_EXCEEDED",
                }
            }
            if unexpected:
                validation_error = (
                    f"T35 expected OUTPUT_LIMIT_EXCEEDED: {unexpected}"
                )
        else:
            failures = {
                execution_id: outcome
                for execution_id, outcome in terminal_outcomes.items()
                if outcome["status"] != "SUCCEEDED"
            }
            if failures:
                raise RuntimeError(f"T35 Executions failed: {failures}")
        retrievals = await _retrieve_results(client, execution_ids)
        if expect_output_limit:
            missing_incomplete_references = [
                item["execution_id"]
                for item in retrievals
                if item["incomplete_step_result_ref_count"] < 1
            ]
            if missing_incomplete_references:
                validation_error = (
                    "T35 output-limit failure has no incomplete Step result "
                    f"reference: {missing_incomplete_references}"
                )
        cleanup_outcomes: dict[str, dict[str, str | None]] = {}
        if expect_output_limit:
            cleanup_outcomes = await _cleanup_output_limit_executions(
                client,
                execution_ids,
                run_id=run_id,
                operation_mode=operation_mode,
                timeout_seconds=timeout_seconds,
            )
            if validation_error is not None:
                raise RuntimeError(validation_error)
    finally:
        stop.set()
        await sampler
    database_after = await database_snapshot(engine)
    rss_baseline = _first(samples, "executor_rss_bytes")
    rss_peak = _maximum(samples, "executor_rss_bytes")
    runtime_baseline = _first(samples, "runtime_memory_bytes")
    runtime_peak = _maximum(samples, "runtime_memory_bytes")
    latencies = [float(item["latency_seconds"]) for item in retrievals]
    return {
        "name": scenario.name,
        "configuration": asdict(scenario),
        "operation_mode": operation_mode,
        "requested_output_bytes_per_execution": scenario.size_mib * MIB,
        "requested_output_bytes_total": (
            scenario.size_mib * MIB * scenario.concurrency
        ),
        "execution_ids": execution_ids,
        "expected_outcome": (
            "OUTPUT_LIMIT_EXCEEDED" if expect_output_limit else "SUCCEEDED"
        ),
        "terminal_outcomes": terminal_outcomes,
        "cleanup_outcomes": cleanup_outcomes,
        "timing_seconds": {
            "submit_total": round(submit_seconds, 6),
            "execution_until_terminal": round(execution_seconds, 6),
        },
        "executor": {
            "rss_baseline_bytes": rss_baseline,
            "rss_peak_bytes": rss_peak,
            "rss_growth_bytes": (
                rss_peak - rss_baseline
                if rss_peak is not None and rss_baseline is not None
                else None
            ),
            "active_execution_peak": max(
                peak_running,
                _maximum(samples, "active_execution_count") or 0,
            ),
        },
        "runtime": {
            "memory_baseline_bytes": runtime_baseline,
            "memory_peak_bytes": runtime_peak,
            "memory_growth_bytes": (
                runtime_peak - runtime_baseline
                if runtime_peak is not None and runtime_baseline is not None
                else None
            ),
            "target_count": _maximum(samples, "runtime_target_count"),
        },
        "postgresql": {
            "before": asdict(database_before),
            "after": asdict(database_after),
            "growth": database_delta(database_before, database_after),
        },
        "agent_retrieval": {
            "call_count": len(retrievals),
            "response_bytes_total": sum(
                int(item["response_size_bytes"]) for item in retrievals
            ),
            "latency_p50_seconds": _percentile(latencies, 0.50),
            "latency_p95_seconds": _percentile(latencies, 0.95),
            "latency_max_seconds": max(latencies, default=None),
            "calls": retrievals,
        },
        "notebook": {
            "size_bytes_total": sum(
                int(item["notebook_size_bytes"] or 0) for item in retrievals
            ),
            "size_bytes_by_execution": {
                str(item["execution_id"]): item["notebook_size_bytes"]
                for item in retrievals
            },
        },
        "resource_samples": [asdict(sample) for sample in samples],
        "sample_errors": sorted(
            {error for sample in samples for error in sample.errors}
        ),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preset", choices=("smoke", "full"), default="smoke")
    parser.add_argument(
        "--scenario",
        action="append",
        type=parse_scenario,
        help=(
            "Run an explicit TYPE:SIZE_MIB:CONCURRENCY scenario. May be "
            "repeated and overrides --preset."
        ),
    )
    parser.add_argument(
        "--confirm-full",
        action="store_true",
        help="Acknowledge the full matrix's substantial resource usage.",
    )
    parser.add_argument(
        "--allow-missing-executor-rss",
        action="store_true",
        help="Continue without Docker Executor RSS measurements.",
    )
    parser.add_argument(
        "--allow-queued-concurrency",
        action="store_true",
        help=(
            "Allow requested concurrency to exceed configured Runtime "
            "capacity. Such a run measures queued submissions, not active "
            "output concurrency."
        ),
    )
    parser.add_argument(
        "--expect-output-limit",
        action="store_true",
        help=(
            "Require every explicit scenario Execution to fail with "
            "OUTPUT_LIMIT_EXCEEDED. The Executor limit must be lower than "
            "the generated Runtime WebSocket message."
        ),
    )
    parser.add_argument(
        "--operation-mode",
        choices=("SINGLE", "MULTI"),
        default="SINGLE",
        help="Execution lifecycle used by every scenario.",
    )
    parser.add_argument("--sample-interval-seconds", type=float, default=1.0)
    parser.add_argument("--scenario-timeout-seconds", type=float, default=3600)
    parser.add_argument("--cooldown-seconds", type=float, default=2.0)
    args = parser.parse_args()
    if args.preset == "full" and not args.scenario and not args.confirm_full:
        parser.error("--preset full requires --confirm-full.")
    if args.expect_output_limit and not args.scenario:
        parser.error("--expect-output-limit requires an explicit --scenario.")
    if args.operation_mode == "MULTI" and not args.expect_output_limit:
        parser.error(
            "--operation-mode MULTI is reserved for --expect-output-limit; "
            "successful MULTI work waits for another Operation by design."
        )
    if args.sample_interval_seconds <= 0:
        parser.error("--sample-interval-seconds must be positive.")
    if args.scenario_timeout_seconds <= 0:
        parser.error("--scenario-timeout-seconds must be positive.")
    if args.cooldown_seconds < 0:
        parser.error("--cooldown-seconds must be non-negative.")
    return args


def _require_safe_executor_url(url: str) -> None:
    hostname = urlparse(url).hostname
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return
    if os.getenv("T35_ALLOW_REMOTE", "false").strip().lower() in {
        "1",
        "true",
        "yes",
    }:
        return
    raise RuntimeError(
        "T35 refuses a non-loopback Executor by default. Set "
        "T35_ALLOW_REMOTE=true only for an isolated non-production test stack."
    )


async def _eligible_targets(
    client: httpx.AsyncClient,
) -> tuple[list[dict[str, Any]], int]:
    response = await client.get(
        "/api/v1/runtime-targets", params={"limit": 200}
    )
    response.raise_for_status()
    configured_ids = {
        value.strip()
        for value in os.getenv("T35_RUNTIME_TARGET_IDS", "").split(",")
        if value.strip()
    }
    targets = [
        item
        for item in response.json()["items"]
        if item["runtime"]["type"] == "JUPYTER"
        and item["runtime"]["pool"] == "INTERACTIVE"
        and "basic" in item["runtime"]["supported_profiles"]
        and item["state"]["status"] == "ACTIVE"
        and item["state"]["enabled"] is True
        and (not configured_ids or str(item["target_id"]) in configured_ids)
    ]
    if not targets:
        raise RuntimeError(
            "No ACTIVE INTERACTIVE JUPYTER Runtime Target supporting basic "
            "was found. Run local_test_preflight.py or set "
            "T35_RUNTIME_TARGET_IDS."
        )
    capacity = sum(
        int(item["capacity"]["max_concurrent_executions"]) for item in targets
    )
    return targets, capacity


async def main() -> None:
    args = _parse_args()
    scenarios = tuple(args.scenario or scenario_matrix(args.preset))
    run_id = uuid4().hex
    max_concurrency = max(item.concurrency for item in scenarios)
    http_url = executor_http_url()
    _require_safe_executor_url(http_url)
    container: str | None = None
    if args.allow_missing_executor_rss:
        try:
            container = await resolve_executor_container()
        except Exception:
            container = None
    else:
        container = await resolve_executor_container()

    configured_output_limit_bytes = (
        await executor_output_limit_bytes(container)
        if container is not None
        else None
    )

    engine = _database_engine()
    results: list[dict[str, Any]] = []
    status = "RUNNING"
    failure: dict[str, str] | None = None
    report = None
    target_report: list[dict[str, Any]] = []
    try:
        limits = httpx.Limits(
            max_connections=max(100, max_concurrency * 4),
            max_keepalive_connections=max(20, max_concurrency * 2),
        )
        async with httpx.AsyncClient(
            base_url=http_url, timeout=60, limits=limits
        ) as http_client:
            targets, configured_capacity = await _eligible_targets(http_client)
            if (
                max_concurrency > configured_capacity
                and not args.allow_queued_concurrency
            ):
                raise RuntimeError(
                    f"Requested concurrency {max_concurrency} exceeds "
                    f"configured Runtime capacity {configured_capacity}. "
                    "Raise test-target capacity or pass "
                    "--allow-queued-concurrency to measure queued load."
                )
            target_ids = {str(item["target_id"]) for item in targets}
            target_report = [
                {
                    "target_id": item["target_id"],
                    "name": item["name"],
                    "capacity": item["capacity"]["max_concurrent_executions"],
                }
                for item in targets
            ]
            for index, scenario in enumerate(scenarios, start=1):
                print(
                    f"[{index}/{len(scenarios)}] {scenario.name}",
                    flush=True,
                )
                results.append(
                    await run_scenario(
                        scenario,
                        run_id=run_id,
                        client=http_client,
                        engine=engine,
                        container=container,
                        target_ids=target_ids,
                        sample_interval_seconds=args.sample_interval_seconds,
                        timeout_seconds=args.scenario_timeout_seconds,
                        expect_output_limit=args.expect_output_limit,
                        operation_mode=args.operation_mode,
                    )
                )
                report = write_report(
                    "t35-output-measurement",
                    run_id,
                    {
                        "status": status,
                        "preset": args.preset,
                        "scenario_count": len(scenarios),
                        "completed_scenario_count": len(results),
                        "executor_container": container,
                        "runtime_max_output_message_bytes": (
                            configured_output_limit_bytes
                        ),
                        "expect_output_limit": args.expect_output_limit,
                        "operation_mode": args.operation_mode,
                        "runtime_targets": target_report,
                        "results": results,
                    },
                )
                if index < len(scenarios):
                    await asyncio.sleep(args.cooldown_seconds)
    except Exception as exc:
        status = "FAILED"
        failure = {"type": type(exc).__name__, "message": str(exc)}
        raise
    else:
        status = "PASSED"
    finally:
        await engine.dispose()
        report = write_report(
            "t35-output-measurement",
            run_id,
            {
                "status": status,
                "preset": args.preset,
                "scenario_count": len(scenarios),
                "completed_scenario_count": len(results),
                "executor_container": container,
                "runtime_max_output_message_bytes": (
                    configured_output_limit_bytes
                ),
                "expect_output_limit": args.expect_output_limit,
                "operation_mode": args.operation_mode,
                "runtime_targets": target_report,
                "failure": failure,
                "results": results,
            },
        )
        print(f"status: {status}", flush=True)
        print(f"report: {report}", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
