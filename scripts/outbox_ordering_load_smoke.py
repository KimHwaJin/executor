"""Run ordered Outbox backlog scenarios against disposable PostgreSQL databases."""

from __future__ import annotations

import argparse
import os
import subprocess
from dataclasses import dataclass

TEST = "tests/test_multi_worker_postgres.py::test_ordered_outbox_backlog_load"


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    execution_count: int
    events_per_execution: int


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--single-events", type=int, default=2000)
    parser.add_argument("--parallel-executions", type=int, default=30)
    parser.add_argument("--parallel-events", type=int, default=100)
    parser.add_argument("--min-events-per-second", type=float, default=0)
    return parser.parse_args()


def _run(scenario: Scenario, minimum_events_per_second: float) -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "EXECUTOR_RUN_POSTGRES_TESTS": "1",
            "EXECUTOR_REQUIRE_REDIS_TESTS": "1",
            "EXECUTOR_OUTBOX_LOAD_EXECUTION_COUNT": str(
                scenario.execution_count
            ),
            "EXECUTOR_OUTBOX_LOAD_EVENTS_PER_EXECUTION": str(
                scenario.events_per_execution
            ),
            "EXECUTOR_OUTBOX_LOAD_MIN_EVENTS_PER_SECOND": str(
                minimum_events_per_second
            ),
        }
    )
    print(f"\n==> {scenario.name}", flush=True)
    subprocess.run(
        ("uv", "run", "pytest", "-q", "-s", TEST),
        env=environment,
        check=True,
    )


def main() -> None:
    args = _parse_args()
    scenarios = (
        Scenario("single-execution-backlog", 1, args.single_events),
        Scenario(
            "parallel-execution-backlog",
            args.parallel_executions,
            args.parallel_events,
        ),
    )
    for scenario in scenarios:
        _run(scenario, args.min_events_per_second)


if __name__ == "__main__":
    main()
