"""Run deterministic Executor quality gates on macOS, Linux, or Windows."""

import argparse
import os
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    command: tuple[str, ...]
    environment: dict[str, str] | None = None


def _base_checks() -> tuple[Check, ...]:
    return (
        Check("ruff-lint", ("uv", "run", "ruff", "check", ".")),
        Check(
            "ruff-format",
            ("uv", "run", "ruff", "format", "--check", "."),
        ),
        Check("type-check", ("uv", "run", "ty", "check")),
        Check(
            "unit-tests",
            (
                "uv",
                "run",
                "pytest",
                "-q",
                "--ignore=tests/test_event_delivery.py",
                "--ignore=tests/test_multi_worker_postgres.py",
            ),
        ),
    )


def _integration_checks() -> tuple[Check, ...]:
    environment = {
        "EXECUTOR_REQUIRE_REDIS_TESTS": "1",
        "EXECUTOR_RUN_POSTGRES_TESTS": "1",
    }
    return (
        Check(
            "redis-integration",
            (
                "uv",
                "run",
                "pytest",
                "-q",
                "tests/test_event_delivery.py",
            ),
            environment,
        ),
        Check(
            "postgres-integration-and-migrations",
            (
                "uv",
                "run",
                "pytest",
                "-q",
                "tests/test_multi_worker_postgres.py",
            ),
            environment,
        ),
    )


def _run(check: Check) -> None:
    environment = os.environ.copy()
    environment.update(check.environment or {})
    print(f"\n==> {check.name}", flush=True)
    subprocess.run(check.command, env=environment, check=True)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--integration",
        action="store_true",
        help=(
            "Also require real Redis and disposable PostgreSQL integration "
            "tests, including Alembic upgrade and schema checks."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    checks = list(_base_checks())
    if args.integration:
        checks.extend(_integration_checks())
    for check in checks:
        _run(check)
    print("\nAll requested Executor quality gates passed.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode) from exc
    except KeyboardInterrupt:
        raise SystemExit(130) from None
