"""Run the local Executor validation phases and retain command output as test evidence."""

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class Step:
    name: str
    script: str
    environment: dict[str, str]
    requires_exclusive_executor: bool = False


def _steps(args: argparse.Namespace) -> list[Step]:
    common = [
        Step("preflight", "local_test_preflight.py", {}),
        Step(
            "single-observability",
            "single_execution_observability_smoke.py",
            {},
        ),
        Step(
            "output-journal-storage",
            "output_journal_storage_e2e.py",
            {},
        ),
    ]
    if args.full:
        common.extend(
            [
                Step(
                    "single-lifecycle",
                    "single_failure_retry_cancel_e2e.py",
                    {},
                ),
                Step(
                    "multi-lifecycle", "multi_execution_lifecycle_e2e.py", {}
                ),
            ]
        )
    common.extend(
        [
            Step(
                "mixed-output-load",
                "mixed_output_load_smoke.py",
                {"MIXED_LOAD_EXECUTION_COUNT": str(args.mixed_count)},
            ),
            Step(
                "long-running-soak",
                "jupyter_long_running_soak.py",
                {
                    "SOAK_DURATION_SECONDS": str(args.soak_seconds),
                    "SOAK_OUTPUT_INTERVAL_SECONDS": str(
                        args.soak_output_interval_seconds
                    ),
                },
            ),
        ]
    )
    if args.full:
        common.append(
            Step(
                "concurrent-load",
                "multi_executor_load_smoke.py",
                {
                    "RESILIENCE_EXECUTION_COUNT": str(args.load_count),
                    "RESILIENCE_CELL_SLEEP_SECONDS": str(
                        args.load_cell_sleep_seconds
                    ),
                },
                requires_exclusive_executor=True,
            )
        )
    if args.include_faults:
        common.extend(
            [
                Step(
                    "graceful-drain",
                    "multi_executor_drain_smoke.py",
                    {},
                    requires_exclusive_executor=True,
                ),
                Step(
                    "forced-process-loss",
                    "multi_executor_failover_smoke.py",
                    {},
                    requires_exclusive_executor=True,
                ),
                Step(
                    "redis-outage",
                    "executor_redis_outage_smoke.py",
                    {},
                    requires_exclusive_executor=True,
                ),
                Step(
                    "jupyter-outage",
                    "jupyter_server_outage_smoke.py",
                    {"ALLOW_DOCKER_JUPYTER_OUTAGE_TEST": "1"},
                    requires_exclusive_executor=True,
                ),
            ]
        )
    return common


def _compose(*arguments: str) -> None:
    command = ["docker", "compose"]
    configured_files = os.getenv("LOCAL_TEST_COMPOSE_FILES", "").strip()
    if configured_files:
        for compose_file in configured_files.split(os.pathsep):
            command.extend(["-f", compose_file])
    subprocess.run([*command, *arguments], check=True)


def _run_step(step: Step, result_dir: Path) -> dict[str, object]:
    environment = os.environ.copy()
    environment.update(step.environment)
    environment["LOCAL_TEST_RESULTS_DIR"] = str(result_dir)
    log_path = result_dir / f"{step.name}.log"
    started = monotonic()
    with log_path.open("w", encoding="utf-8") as log:
        completed = subprocess.run(
            [sys.executable, str(Path("scripts") / step.script)],
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
            text=True,
        )
    duration = monotonic() - started
    result = {
        "name": step.name,
        "script": step.script,
        "status": "PASSED" if completed.returncode == 0 else "FAILED",
        "exit_code": completed.returncode,
        "duration_seconds": round(duration, 3),
        "log": str(log_path),
    }
    if completed.returncode != 0:
        result["error_tail"] = log_path.read_text(encoding="utf-8")[-4000:]
    return result


def _run_required_step(
    step: Step,
    result_dir: Path,
    step_results: list[dict[str, object]],
) -> None:
    result = _run_step(step, result_dir)
    step_results.append(result)
    if result["status"] != "PASSED":
        raise RuntimeError(
            f"Validation step {step.name} failed.\n{result.get('error_tail', '')}"
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--full",
        action="store_true",
        help="Include lifecycle and two-Executor concurrent-load scenarios.",
    )
    parser.add_argument(
        "--include-faults",
        action="store_true",
        help="Include disruptive Executor, Redis, and Jupyter fault scenarios.",
    )
    parser.add_argument("--soak-seconds", type=int, default=300)
    parser.add_argument("--soak-output-interval-seconds", type=int, default=60)
    parser.add_argument("--mixed-count", type=int, default=14)
    parser.add_argument("--load-count", type=int, default=30)
    parser.add_argument("--load-cell-sleep-seconds", type=float, default=3.0)
    args = parser.parse_args()
    if args.soak_seconds < 5:
        parser.error("--soak-seconds must be at least 5")
    if not 7 <= args.mixed_count <= 30:
        parser.error("--mixed-count must be between 7 and 30")
    if not 20 <= args.load_count <= 60:
        parser.error("--load-count must be between 20 and 60")
    return args


def main() -> None:
    args = _parse_args()
    run_id = uuid4().hex
    result_dir = Path("test-results") / f"suite-{run_id}"
    result_dir.mkdir(parents=True, exist_ok=False)
    step_results: list[dict[str, object]] = []
    compose_executor_stopped = False
    status = "PASSED"
    error: str | None = None
    pending_error: Exception | None = None
    try:
        for step in _steps(args):
            if (
                step.requires_exclusive_executor
                and not compose_executor_stopped
            ):
                _compose("stop", "executor")
                compose_executor_stopped = True
            elif (
                not step.requires_exclusive_executor
                and compose_executor_stopped
            ):
                _compose("up", "-d", "--wait", "executor")
                compose_executor_stopped = False
                # Real-process tests register host endpoints. Restore Compose-internal endpoints
                # before returning to non-disruptive tests.
                _run_required_step(
                    Step(
                        "restore-compose-targets",
                        "local_test_preflight.py",
                        {},
                    ),
                    result_dir,
                    step_results,
                )
            _run_required_step(step, result_dir, step_results)
    except Exception as exc:
        status = "FAILED"
        error = str(exc)
        pending_error = exc
    finally:
        if compose_executor_stopped:
            try:
                _compose("up", "-d", "--wait", "executor")
                _run_required_step(
                    Step(
                        "restore-compose-targets",
                        "local_test_preflight.py",
                        {},
                    ),
                    result_dir,
                    step_results,
                )
            except Exception as restore_error:
                status = "FAILED"
                error = (
                    f"{error or ''}\nRestore failed: {restore_error}".strip()
                )
                pending_error = pending_error or restore_error
        summary = {
            "schema_version": "1.0",
            "run_id": run_id,
            "recorded_at": datetime.now(UTC).isoformat(),
            "status": status,
            "configuration": {
                "full": args.full,
                "include_faults": args.include_faults,
                "soak_seconds": args.soak_seconds,
                "mixed_count": args.mixed_count,
                "load_count": args.load_count,
            },
            "steps": step_results,
            "error": error,
        }
        summary_path = result_dir / "summary.json"
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print("status:", status)
        print("summary:", summary_path)
    if pending_error is not None:
        raise pending_error


if __name__ == "__main__":
    main()
