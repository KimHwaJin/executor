"""Validate Executor Worker-loss recovery against a Kubernetes deployment.

This script is intentionally destructive: it deletes the Pod that owns a
running Execution. Run it only in an isolated non-production namespace.
"""

import argparse
import asyncio
import json
import os
import ssl
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from execution_spec_payload import execution_request, inline_spec
from worker_failover_assertions import (
    FailoverValidationError as ValidationError,
)
from worker_failover_assertions import (
    validate_attempt_history as _validate_attempt_history,
)
from worker_failover_assertions import (
    validate_event_history as _validate_event_history,
)

TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED"}


@dataclass(frozen=True)
class Config:
    base_url: str
    namespace: str
    deployment: str
    selector: str
    context: str | None
    runtime_profile: str
    step_duration_seconds: int
    lease_timeout_seconds: int
    completion_timeout_seconds: int
    request_timeout_seconds: float
    report_path: Path
    bearer_token: str | None
    ca_file: str | None
    allow_pod_delete: bool


class Kubectl:
    def __init__(self, config: Config) -> None:
        self._config = config

    def _command(self, *arguments: str) -> list[str]:
        command = ["kubectl"]
        if self._config.context:
            command.extend(["--context", self._config.context])
        command.extend(["--namespace", self._config.namespace, *arguments])
        return command

    async def run(self, *arguments: str) -> str:
        command = self._command(*arguments)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as error:
            raise ValidationError("kubectl was not found on PATH.") from error
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            detail = stderr.decode(errors="replace").strip()
            raise ValidationError(
                f"kubectl failed ({' '.join(command)}): {detail}"
            )
        return stdout.decode().strip()

    async def current_context(self) -> str:
        command = ["kubectl", "config", "current-context"]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            detail = stderr.decode(errors="replace").strip()
            raise ValidationError(
                f"Could not read the kubectl context: {detail}"
            )
        return stdout.decode().strip()

    async def deployment(self) -> dict[str, Any]:
        raw = await self.run(
            "get",
            "deployment",
            self._config.deployment,
            "--output=json",
        )
        return json.loads(raw)

    async def pods(self) -> list[dict[str, Any]]:
        raw = await self.run(
            "get",
            "pods",
            "--selector",
            self._config.selector,
            "--output=json",
        )
        return json.loads(raw)["items"]

    async def delete_pod(self, pod_name: str) -> None:
        await self.run(
            "delete",
            "pod",
            pod_name,
            "--grace-period=0",
            "--force",
            "--wait=false",
        )


def _ready_pod_names(pods: list[dict[str, Any]]) -> list[str]:
    ready = []
    for pod in pods:
        conditions = pod.get("status", {}).get("conditions", [])
        if pod.get("status", {}).get("phase") != "Running":
            continue
        if not any(
            condition.get("type") == "Ready"
            and condition.get("status") == "True"
            for condition in conditions
        ):
            continue
        ready.append(str(pod["metadata"]["name"]))
    return sorted(ready)


def _http_verify(config: Config) -> bool | ssl.SSLContext:
    if config.ca_file is None:
        return True
    return ssl.create_default_context(cafile=config.ca_file)


def _headers(config: Config) -> dict[str, str]:
    if config.bearer_token is None:
        return {}
    return {"Authorization": f"Bearer {config.bearer_token}"}


async def _get_json(
    client: httpx.AsyncClient,
    path: str,
    *,
    transient: bool = False,
) -> dict[str, Any] | None:
    try:
        response = await client.get(path)
        response.raise_for_status()
    except (httpx.HTTPError, json.JSONDecodeError):
        if transient:
            return None
        raise
    return response.json()


async def _wait_for_execution(
    client: httpx.AsyncClient,
    execution_id: str,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    timeout_seconds: int,
    description: str,
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last_state: dict[str, Any] | None = None
    while asyncio.get_running_loop().time() < deadline:
        state = await _get_json(
            client,
            f"/api/v1/executions/{execution_id}",
            transient=True,
        )
        if state is not None:
            last_state = state
            if predicate(state):
                return state
        await asyncio.sleep(1)
    raise ValidationError(
        f"Timed out waiting for {description}; last state={last_state}"
    )


async def _attempt_details(
    client: httpx.AsyncClient, execution_id: str
) -> list[dict[str, Any]]:
    page = await _get_json(
        client, f"/api/v1/executions/{execution_id}/attempts?limit=200"
    )
    assert page is not None
    details = []
    for item in page["items"]:
        attempt_id = item["attempt_id"]
        detail = await _get_json(
            client,
            f"/api/v1/executions/{execution_id}/attempts/{attempt_id}",
        )
        assert detail is not None
        details.append(detail)
    return sorted(details, key=lambda item: item["attempt_number"])


async def _events(
    client: httpx.AsyncClient, execution_id: str
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        path = f"/api/v1/executions/{execution_id}/events?limit=500"
        if cursor is not None:
            path += f"&{httpx.QueryParams({'cursor': cursor})}"
        page = await _get_json(client, path)
        assert page is not None
        items.extend(page["items"])
        cursor = page.get("next_cursor")
        if cursor is None:
            return items


async def _preflight(
    config: Config,
    kubectl: Kubectl,
    client: httpx.AsyncClient,
) -> tuple[str, list[str], dict[str, Any]]:
    if not config.allow_pod_delete:
        raise ValidationError(
            "Refusing to delete a Pod without --allow-pod-delete. "
            "Use only in an isolated non-production namespace."
        )
    context = config.context or await kubectl.current_context()
    deployment = await kubectl.deployment()
    desired = int(deployment.get("spec", {}).get("replicas", 1))
    pods = await kubectl.pods()
    ready_pods = _ready_pod_names(pods)
    if desired < 1 or not ready_pods:
        raise ValidationError("The Executor Deployment has no Ready Pod.")
    ready = await _get_json(client, "/readyz")
    if ready is None or ready.get("status") != "ready":
        raise ValidationError(f"Executor is not Ready: {ready}")
    worker = await _get_json(client, "/workerz")
    assert worker is not None
    if not worker.get("accepting_new_executions"):
        raise ValidationError(
            f"Executor Worker is not accepting work: {worker}"
        )
    return context, ready_pods, worker


async def run(config: Config) -> dict[str, Any]:
    kubectl = Kubectl(config)
    unique = uuid4().hex
    async with httpx.AsyncClient(
        base_url=config.base_url.rstrip("/"),
        headers=_headers(config),
        timeout=config.request_timeout_seconds,
        verify=_http_verify(config),
    ) as client:
        context, initial_pods, initial_worker = await _preflight(
            config, kubectl, client
        )
        code = (
            "import time\n"
            f"print('worker-loss-start:{unique}', flush=True)\n"
            f"time.sleep({config.step_duration_seconds})\n"
            f"print('worker-loss-finished:{unique}', flush=True)\n"
        )
        request = execution_request(
            idempotency_key=f"kube-worker-loss-submit-{unique}",
            operation_mode="SINGLE",
            trigger_type="INTERACTIVE",
            actor={"type": "USER", "id": "kube-failover-validator"},
            runtime_profile=config.runtime_profile,
            spec=inline_spec(
                [
                    {
                        "skill_name": "data_io",
                        "tool_name": "kubernetes_worker_loss",
                        "code": code,
                        "step_timeout_seconds": (
                            config.step_duration_seconds + 300
                        ),
                    }
                ]
            ),
            context={
                "user_id": "kube-failover-validator",
                "project_id": "kube-failover-validation",
                "session_id": f"kube-failover-{unique}",
                "task_id": f"kube-failover-{unique}",
            },
            operation_timeout_seconds=config.step_duration_seconds + 360,
        )
        response = await client.post("/api/v1/executions", json=request)
        response.raise_for_status()
        receipt = response.json()
        execution_id = str(receipt["execution_id"])

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
        attempts = await _attempt_details(client, execution_id)
        if len(attempts) != 1 or attempts[0]["lease"]["owner"] is None:
            raise ValidationError(
                f"Could not identify the owning Worker: {attempts}"
            )
        owner = str(attempts[0]["lease"]["owner"])
        if owner not in initial_pods:
            raise ValidationError(
                f"Attempt owner {owner!r} is not a Ready Pod selected by "
                f"{config.selector!r}: {initial_pods}"
            )
        initial_session_id = str(running["runtime"]["session_id"])
        await kubectl.delete_pod(owner)

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
            raise ValidationError(f"Unexpected recovery result: {failed}")
        if failed["retry"]["strategy"] != "FROM_START":
            raise ValidationError(f"Unexpected retry strategy: {failed}")
        if failed["recovery"]["runtime_session_cleanup_status"] != "SUCCEEDED":
            raise ValidationError(f"Runtime cleanup did not succeed: {failed}")
        if failed["runtime"]["session_id"] is not None:
            raise ValidationError(
                "The abandoned Runtime session is still assigned."
            )

        retry = await client.post(
            f"/api/v1/executions/{execution_id}/retry",
            json={
                "idempotency_key": f"kube-worker-loss-retry-{unique}",
                "actor": {
                    "type": "USER",
                    "id": "kube-failover-validator",
                },
            },
        )
        retry.raise_for_status()
        final = await _wait_for_execution(
            client,
            execution_id,
            lambda state: state["state"]["status"] in TERMINAL_STATUSES,
            timeout_seconds=config.completion_timeout_seconds,
            description="the explicit retry to finish",
        )
        if final["state"]["status"] != "SUCCEEDED":
            raise ValidationError(f"Explicit retry did not succeed: {final}")

        final_attempts = await _attempt_details(client, execution_id)
        _validate_attempt_history(
            final_attempts,
            initial_session_id=initial_session_id,
        )
        events = await _events(client, execution_id)
        _validate_event_history(events)
        replacement_pods = _ready_pod_names(await kubectl.pods())
        final_worker = await _get_json(client, "/workerz", transient=True)

    report = {
        "status": "PASSED",
        "occurred_at": datetime.now(UTC).isoformat(),
        "kubernetes": {
            "context": context,
            "namespace": config.namespace,
            "deployment": config.deployment,
            "selector": config.selector,
            "initial_ready_pods": initial_pods,
            "deleted_owner_pod": owner,
            "final_ready_pods": replacement_pods,
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
            "event_count": len(events),
            "event_sequences": [event["event_sequence"] for event in events],
        },
        "worker_before": initial_worker,
        "worker_after": final_worker,
        "configuration": {
            key: value
            for key, value in asdict(config).items()
            if key not in {"bearer_token", "allow_pod_delete"}
        },
    }
    config.report_path.parent.mkdir(parents=True, exist_ok=True)
    config.report_path.write_text(
        json.dumps(report, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return report


def _parse_args() -> Config:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.getenv("KUBE_FAILOVER_EXECUTOR_URL"),
        required=os.getenv("KUBE_FAILOVER_EXECUTOR_URL") is None,
    )
    parser.add_argument(
        "--namespace",
        default=os.getenv("KUBE_FAILOVER_NAMESPACE", "executor"),
    )
    parser.add_argument(
        "--deployment",
        default=os.getenv("KUBE_FAILOVER_DEPLOYMENT", "executor"),
    )
    parser.add_argument(
        "--selector",
        default=os.getenv(
            "KUBE_FAILOVER_SELECTOR",
            "app.kubernetes.io/name=executor,"
            "app.kubernetes.io/component=application",
        ),
    )
    parser.add_argument(
        "--context", default=os.getenv("KUBE_FAILOVER_CONTEXT")
    )
    parser.add_argument(
        "--runtime-profile",
        default=os.getenv("KUBE_FAILOVER_RUNTIME_PROFILE", "basic"),
    )
    parser.add_argument("--step-duration-seconds", type=int, default=180)
    parser.add_argument("--lease-timeout-seconds", type=int, default=240)
    parser.add_argument("--completion-timeout-seconds", type=int, default=600)
    parser.add_argument("--request-timeout-seconds", type=float, default=30)
    parser.add_argument(
        "--report-path",
        type=Path,
        default=Path("test-results/kubernetes-worker-failover.json"),
    )
    parser.add_argument(
        "--ca-file", default=os.getenv("KUBE_FAILOVER_CA_FILE")
    )
    parser.add_argument(
        "--allow-pod-delete",
        action="store_true",
        help="Required acknowledgement that the owning Pod will be force deleted.",
    )
    arguments = parser.parse_args()
    if arguments.step_duration_seconds < 30:
        parser.error("--step-duration-seconds must be at least 30")
    if arguments.lease_timeout_seconds < 60:
        parser.error("--lease-timeout-seconds must be at least 60")
    return Config(
        base_url=arguments.base_url,
        namespace=arguments.namespace,
        deployment=arguments.deployment,
        selector=arguments.selector,
        context=arguments.context,
        runtime_profile=arguments.runtime_profile,
        step_duration_seconds=arguments.step_duration_seconds,
        lease_timeout_seconds=arguments.lease_timeout_seconds,
        completion_timeout_seconds=arguments.completion_timeout_seconds,
        request_timeout_seconds=arguments.request_timeout_seconds,
        report_path=arguments.report_path,
        bearer_token=os.getenv("KUBE_FAILOVER_BEARER_TOKEN"),
        ca_file=arguments.ca_file,
        allow_pod_delete=arguments.allow_pod_delete,
    )


def main() -> None:
    try:
        report = asyncio.run(run(_parse_args()))
    except (ValidationError, httpx.HTTPError) as error:
        print(f"FAILED: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
