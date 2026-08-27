"""Run an isolated two-Executor Worker-loss E2E test with Docker Compose."""

import argparse
import asyncio
import json
import os
import re
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from execution_spec_payload import execution_request, inline_spec
from mcp import Client
from resilience_common import (
    attempts,
    events,
    execution,
    upsert_runtime_target,
)
from worker_failover_assertions import (
    FailoverValidationError,
    validate_attempt_history,
    validate_event_history,
)

PRIMARY_CONSUMER = "docker-failover-primary"
SECONDARY_CONSUMER = "docker-failover-secondary"
PRIMARY_SERVICE = "executor-primary"
SECONDARY_SERVICE = "executor-secondary"
TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED"}


@dataclass(frozen=True)
class Config:
    compose_file: Path
    project_name: str
    primary_port: int
    secondary_port: int
    runtime_profile: str
    step_duration_seconds: int
    lease_timeout_seconds: int
    completion_timeout_seconds: int
    report_path: Path
    jupyter_token: str
    allow_container_kill: bool
    keep_stack: bool
    build_jupyter_image: bool


class Compose:
    def __init__(self, config: Config) -> None:
        self._config = config

    def _command(self, *arguments: str) -> list[str]:
        return [
            "docker",
            "compose",
            "--project-name",
            self._config.project_name,
            "--file",
            str(self._config.compose_file),
            *arguments,
        ]

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update(
            {
                "DOCKER_FAILOVER_PRIMARY_PORT": str(self._config.primary_port),
                "DOCKER_FAILOVER_SECONDARY_PORT": str(
                    self._config.secondary_port
                ),
                "DOCKER_FAILOVER_JUPYTER_TOKEN": (self._config.jupyter_token),
            }
        )
        return environment

    async def run(
        self, *arguments: str, tolerate_failure: bool = False
    ) -> str:
        command = self._command(*arguments)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                env=self._environment(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as error:
            raise FailoverValidationError(
                "docker was not found on PATH."
            ) from error
        stdout, stderr = await process.communicate()
        output = stdout.decode(errors="replace").strip()
        if process.returncode != 0 and not tolerate_failure:
            detail = stderr.decode(errors="replace").strip()
            raise FailoverValidationError(
                f"Docker Compose failed ({' '.join(command)}): "
                f"{detail[-4000:]}"
            )
        return output

    async def up(self) -> None:
        await self.run("build", "migrate")
        if self._config.build_jupyter_image:
            await self.run("build", "jupyter")
        await self.run("up", "--detach")

    async def kill(self, service: str) -> None:
        await self.run("kill", "--signal", "SIGKILL", service)

    async def down(self) -> None:
        await self.run(
            "down",
            "--volumes",
            "--remove-orphans",
            "--timeout",
            "5",
            tolerate_failure=True,
        )


async def _wait_ready(port: int, *, timeout_seconds: int = 180) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    url = f"http://127.0.0.1:{port}/readyz"
    async with httpx.AsyncClient(timeout=2) as client:
        while asyncio.get_running_loop().time() < deadline:
            try:
                response = await client.get(url)
                if response.status_code == 200:
                    body = response.json()
                    if body.get("status") == "ready":
                        return
            except (httpx.HTTPError, json.JSONDecodeError):
                pass
            await asyncio.sleep(1)
    raise FailoverValidationError(
        f"Executor on port {port} did not become Ready."
    )


async def _wait_for_execution(
    client: Client,
    execution_id: str,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    timeout_seconds: int,
    description: str,
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last_state: dict[str, Any] | None = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            state = await execution(client, execution_id)
        except ExceptionGroup:
            await asyncio.sleep(1)
            continue
        last_state = state
        if predicate(state):
            return state
        await asyncio.sleep(1)
    raise FailoverValidationError(
        f"Timed out waiting for {description}; last state={last_state}"
    )


async def _submit(client: Client, config: Config, unique: str) -> str:
    code = (
        "import time\n"
        f"print('docker-worker-loss-start:{unique}', flush=True)\n"
        f"time.sleep({config.step_duration_seconds})\n"
        f"print('docker-worker-loss-finished:{unique}', flush=True)\n"
    )
    result = await client.call_tool(
        "execution_submit",
        {
            "request": execution_request(
                idempotency_key=f"docker-worker-loss-submit-{unique}",
                operation_mode="SINGLE",
                trigger_type="INTERACTIVE",
                actor={
                    "type": "USER",
                    "id": "docker-failover-validator",
                },
                runtime_profile=config.runtime_profile,
                spec=inline_spec(
                    [
                        {
                            "skill_name": "data_io",
                            "tool_name": "docker_worker_loss",
                            "code": code,
                            "step_timeout_seconds": (
                                config.step_duration_seconds + 300
                            ),
                        }
                    ]
                ),
                context={
                    "user_id": "docker-failover-validator",
                    "project_id": "docker-failover-validation",
                    "session_id": f"docker-failover-{unique}",
                    "task_id": f"docker-failover-{unique}",
                },
                operation_timeout_seconds=(config.step_duration_seconds + 360),
            )
        },
    )
    if result.is_error:
        raise FailoverValidationError(
            f"Execution submission failed: {result.content}"
        )
    return str(result.structured_content["execution_id"])


def _owner_target(owner: str, config: Config) -> tuple[str, str]:
    if owner == PRIMARY_CONSUMER:
        return PRIMARY_SERVICE, f"http://127.0.0.1:{config.secondary_port}"
    if owner == SECONDARY_CONSUMER:
        return SECONDARY_SERVICE, f"http://127.0.0.1:{config.primary_port}"
    raise FailoverValidationError(f"Unexpected Attempt owner: {owner}")


async def run(config: Config) -> dict[str, Any]:
    if not config.allow_container_kill:
        raise FailoverValidationError(
            "Refusing to kill an Executor container without "
            "--allow-container-kill."
        )
    compose = Compose(config)
    unique = uuid4().hex
    execution_id = ""
    killed_service = ""
    started = False
    try:
        print(f"Starting isolated Compose project {config.project_name}...")
        started = True
        await compose.up()
        await asyncio.gather(
            _wait_ready(config.primary_port),
            _wait_ready(config.secondary_port),
        )
        primary_url = f"http://127.0.0.1:{config.primary_port}"
        async with Client(f"{primary_url}/mcp") as client:
            await upsert_runtime_target(
                client,
                unique=unique,
                name="docker-failover-jupyter",
                endpoint="http://jupyter:8888",
                pool="INTERACTIVE",
                token=config.jupyter_token,
                capacity=2,
            )
            execution_id = await _submit(client, config, unique)
            running = await _wait_for_execution(
                client,
                execution_id,
                lambda state: (
                    state["state"]["status"] == "RUNNING"
                    and state["runtime"]["session_id"] is not None
                ),
                timeout_seconds=config.completion_timeout_seconds,
                description="the initial Execution to run",
            )
            initial_attempts = await attempts(client, execution_id)
            if (
                len(initial_attempts) != 1
                or initial_attempts[0]["lease"]["owner"] is None
            ):
                raise FailoverValidationError(
                    f"Could not identify the owning Worker: {initial_attempts}"
                )
            owner = str(initial_attempts[0]["lease"]["owner"])
            killed_service, survivor_url = _owner_target(owner, config)
            initial_session_id = str(running["runtime"]["session_id"])

        print(f"Killing owning service {killed_service} ({owner})...")
        await compose.kill(killed_service)
        survivor_port = (
            config.secondary_port
            if survivor_url.endswith(str(config.secondary_port))
            else config.primary_port
        )
        await _wait_ready(survivor_port)
        async with Client(f"{survivor_url}/mcp") as client:
            failed = await _wait_for_execution(
                client,
                execution_id,
                lambda state: (
                    state["state"]["status"] == "FAILED"
                    and state["recovery"]["runtime_session_cleanup_status"]
                    != "PENDING"
                ),
                timeout_seconds=config.lease_timeout_seconds,
                description="lease fencing and Runtime cleanup",
            )
            if failed["failure"]["type"] != "LEASE_EXPIRED":
                raise FailoverValidationError(
                    f"Unexpected recovery result: {failed}"
                )
            if failed["retry"]["strategy"] != "FROM_START":
                raise FailoverValidationError(
                    f"Unexpected retry strategy: {failed}"
                )
            retry = await client.call_tool(
                "execution_retry",
                {
                    "request": {
                        "execution_id": execution_id,
                        "idempotency_key": (
                            f"docker-worker-loss-retry-{unique}"
                        ),
                        "actor": {
                            "type": "USER",
                            "id": "docker-failover-validator",
                        },
                    }
                },
            )
            if retry.is_error:
                raise FailoverValidationError(
                    f"Explicit retry failed: {retry.content}"
                )
            final = await _wait_for_execution(
                client,
                execution_id,
                lambda state: state["state"]["status"] in TERMINAL_STATUSES,
                timeout_seconds=config.completion_timeout_seconds,
                description="the explicit retry to finish",
            )
            if final["state"]["status"] != "SUCCEEDED":
                raise FailoverValidationError(
                    f"Explicit retry did not succeed: {final}"
                )
            final_attempts = sorted(
                await attempts(client, execution_id),
                key=lambda attempt: attempt["attempt_number"],
            )
            validate_attempt_history(
                final_attempts,
                initial_session_id=initial_session_id,
            )
            history = await events(client, execution_id)
            validate_event_history(history)

        report = {
            "status": "PASSED",
            "occurred_at": datetime.now(UTC).isoformat(),
            "compose": {
                "project_name": config.project_name,
                "compose_file": str(config.compose_file),
                "killed_service": killed_service,
                "killed_owner": owner,
                "survivor_url": survivor_url,
            },
            "execution": {
                "execution_id": execution_id,
                "initial_runtime_session_id": initial_session_id,
                "failure_type": failed["failure"]["type"],
                "retry_strategy": failed["retry"]["strategy"],
                "runtime_session_cleanup_status": failed["recovery"][
                    "runtime_session_cleanup_status"
                ],
                "attempt_owners": [
                    attempt["lease"]["owner"] for attempt in final_attempts
                ],
                "attempt_statuses": [
                    attempt["state"]["status"] for attempt in final_attempts
                ],
                "final_status": final["state"]["status"],
                "event_count": len(history),
                "event_sequences": [
                    event["event_sequence"] for event in history
                ],
            },
            "configuration": {
                key: value
                for key, value in asdict(config).items()
                if key
                not in {
                    "jupyter_token",
                    "allow_container_kill",
                    "keep_stack",
                }
            },
        }
        config.report_path.parent.mkdir(parents=True, exist_ok=True)
        config.report_path.write_text(
            json.dumps(report, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        return report
    finally:
        if started and not config.keep_stack:
            print(
                f"Removing isolated Compose project {config.project_name}..."
            )
            await compose.down()


def _parse_args() -> Config:
    repository_root = Path(__file__).resolve().parents[1]
    unique = uuid4().hex[:8]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--compose-file",
        type=Path,
        default=repository_root / "compose.worker-failover.yaml",
    )
    parser.add_argument(
        "--project-name",
        default=f"executor-worker-failover-{unique}",
    )
    parser.add_argument("--primary-port", type=int, default=8010)
    parser.add_argument("--secondary-port", type=int, default=8011)
    parser.add_argument("--runtime-profile", default="basic")
    parser.add_argument("--step-duration-seconds", type=int, default=90)
    parser.add_argument("--lease-timeout-seconds", type=int, default=180)
    parser.add_argument("--completion-timeout-seconds", type=int, default=300)
    parser.add_argument(
        "--report-path",
        type=Path,
        default=Path("test-results/docker-worker-failover.json"),
    )
    parser.add_argument(
        "--allow-container-kill",
        action="store_true",
        help="Required acknowledgement that one isolated Executor is killed.",
    )
    parser.add_argument(
        "--keep-stack",
        action="store_true",
        help="Retain the isolated Compose project and volumes for diagnosis.",
    )
    parser.add_argument(
        "--build-jupyter-image",
        action="store_true",
        help="Rebuild the Jupyter image instead of reusing the local image.",
    )
    arguments = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", arguments.project_name):
        parser.error("--project-name must match [a-z0-9][a-z0-9_-]*")
    if arguments.primary_port == arguments.secondary_port:
        parser.error("Primary and secondary ports must be different.")
    if not all(
        1 <= port <= 65535
        for port in (arguments.primary_port, arguments.secondary_port)
    ):
        parser.error("Executor ports must be between 1 and 65535.")
    if arguments.step_duration_seconds < 30:
        parser.error("--step-duration-seconds must be at least 30")
    return Config(
        compose_file=arguments.compose_file.resolve(),
        project_name=arguments.project_name,
        primary_port=arguments.primary_port,
        secondary_port=arguments.secondary_port,
        runtime_profile=arguments.runtime_profile,
        step_duration_seconds=arguments.step_duration_seconds,
        lease_timeout_seconds=arguments.lease_timeout_seconds,
        completion_timeout_seconds=arguments.completion_timeout_seconds,
        report_path=arguments.report_path,
        jupyter_token=os.getenv(
            "DOCKER_FAILOVER_JUPYTER_TOKEN", "change-me-failover-only"
        ),
        allow_container_kill=arguments.allow_container_kill,
        keep_stack=arguments.keep_stack,
        build_jupyter_image=arguments.build_jupyter_image,
    )


def _exception_group_message(group: BaseExceptionGroup) -> str:
    messages: list[str] = []
    pending: list[BaseException] = [group]
    while pending:
        error = pending.pop()
        if isinstance(error, BaseExceptionGroup):
            pending.extend(error.exceptions)
        else:
            messages.append(str(error))
    return "; ".join(message for message in messages if message)


def main() -> None:
    try:
        report = asyncio.run(run(_parse_args()))
    except BaseExceptionGroup as error:
        print(f"FAILED: {_exception_group_message(error)}", file=sys.stderr)
        raise SystemExit(1) from error
    except (FailoverValidationError, httpx.HTTPError) as error:
        print(f"FAILED: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
