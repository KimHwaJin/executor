"""Isolated real-process cancel / SIGTERM E2E with text and PNG evidence.

Requires the existing executor-service:local and executor-jupyter:local images.
Never stops normal local services. The generated project owns all its volumes.
"""

import argparse
import asyncio
import json
import secrets
import socket
import subprocess
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from docker_worker_failover_e2e import Compose, Config, _wait_ready
from execution_spec_payload import execution_request, inline_spec
from interrupted_result_assertions import validate_evidence
from mcp import Client
from resilience_common import upsert_runtime_target

REPOSITORY = Path(__file__).resolve().parents[1]
SERVICES = ("executor-primary", "executor-secondary")
CONSUMERS = {
    "docker-failover-primary": SERVICES[0],
    "docker-failover-secondary": SERVICES[1],
}
ACTOR = {"type": "USER", "id": "interruption-e2e"}
JUPYTER_PROBE = """
import json, os, urllib.request
request = urllib.request.Request(
    'http://127.0.0.1:8888/api/kernels',
    headers={'Authorization': 'token ' + os.environ['JUPYTER_TOKEN']})
with urllib.request.urlopen(request, timeout=5) as response:
    print(json.dumps(json.load(response)))
"""


class InterruptionCompose(Compose):
    def _command(self, *arguments: str) -> list[str]:
        return super()._command(
            "--file",
            str(REPOSITORY / "compose.interrupted-results.yaml"),
            *arguments,
        )


def available_ports() -> tuple[int, int]:
    with socket.socket() as first, socket.socket() as second:
        first.bind(("127.0.0.1", 0))
        second.bind(("127.0.0.1", 0))
        return first.getsockname()[1], second.getsockname()[1]


def case_request(mode: str, profile: str, unique: str) -> dict[str, Any]:
    code = (
        "import time\n"
        "import matplotlib.pyplot as plt\n"
        "from IPython.display import display\n"
        "fig, ax = plt.subplots(figsize=(5, 3))\n"
        "ax.bar(['load', 'train', 'evaluate'], [4, 7, 3])\n"
        "ax.set_title('Executor interruption evidence')\n"
        "display(fig)\n"
        "plt.close(fig)\n"
        f"print('before-interrupt:{unique}', flush=True)\n"
        "time.sleep(180)\n"
        f"print('after-interrupt:{unique}', flush=True)\n"
    )
    return execution_request(
        idempotency_key=unique,
        operation_mode=mode,
        trigger_type="INTERACTIVE",
        actor=ACTOR,
        runtime_profile=profile,
        context={"task_id": unique},
        operation_wait_timeout_seconds=300 if mode == "MULTI" else None,
        operation_timeout_seconds=300,
        spec=inline_spec(
            [
                {"code": "print('completed-before-interrupt', flush=True)"},
                {"code": code, "step_timeout_seconds": 240},
                {"code": "print('must-never-run', flush=True)"},
            ]
        ),
    )


async def request(
    client: httpx.AsyncClient, method: str, path: str, **kwargs: Any
) -> Any:
    response = await client.request(method, path, **kwargs)
    response.raise_for_status()
    return response.json()


async def probe(
    compose: Compose, service: str, action: str, execution_id: str
) -> dict[str, Any]:
    output = await compose.run(
        "exec",
        "-T",
        service,
        "python",
        "/opt/tests/probe.py",
        action,
        execution_id,
    )
    return json.loads(output)


async def kernels(compose: Compose) -> list[dict[str, Any]]:
    return json.loads(
        await compose.run(
            "exec", "-T", "jupyter", "python", "-c", JUPYTER_PROBE
        )
    )


async def event_history(
    client: httpx.AsyncClient, execution_id: str
) -> list[dict[str, Any]]:
    items = []
    params: dict[str, Any] = {"limit": 200}
    while True:
        page = await request(
            client,
            "GET",
            f"/api/v1/executions/{execution_id}/events",
            params=params,
        )
        items.extend(page["items"])
        if not page["has_more"]:
            return items
        params["cursor"] = page["next_cursor"]


async def stop_owner(compose: Compose, service: str) -> None:
    if service not in SERVICES:
        raise ValueError("Refusing to stop an unmanaged service")
    await compose.run("stop", "--timeout", "45", service)
    container_id = await compose.run("ps", "--all", "--quiet", service)
    process = await asyncio.create_subprocess_exec(
        "docker",
        "inspect",
        "--format",
        "{{json .State}}",
        container_id,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError("Cannot inspect the stopped test container")
    state = json.loads(stdout)
    if state["Running"] or state["OOMKilled"] or state["ExitCode"] != 0:
        raise RuntimeError(f"Executor did not exit gracefully: {state}")


async def run_case(
    compose: Compose, config: Config, action: str, mode: str, profile: str
) -> dict[str, Any]:
    start = time.monotonic()
    unique = f"interrupt-{uuid4().hex}"
    urls = {
        SERVICES[0]: f"http://127.0.0.1:{config.primary_port}",
        SERVICES[1]: f"http://127.0.0.1:{config.secondary_port}",
    }
    assert await kernels(compose) == [], "Leaked kernel before case"
    async with httpx.AsyncClient(
        base_url=urls[SERVICES[0]], timeout=10
    ) as api:
        submitted = await request(
            api,
            "POST",
            "/api/v1/executions",
            json=case_request(mode, profile, unique),
        )
        execution_id = submitted["execution_id"]
        path = f"/api/v1/executions/{execution_id}"
        print(f"  {action}/{mode}/{profile}: {execution_id}", flush=True)
        async with asyncio.timeout(100):
            while not (
                await probe(compose, SERVICES[0], "progress", execution_id)
            )["ready"]:
                detail = await request(api, "GET", path)
                if detail["state"]["status"] in {"FAILED", "CANCELLED"}:
                    raise AssertionError(f"Failed before output: {detail}")
                await asyncio.sleep(0.5)
        attempts = await request(api, "GET", f"{path}/attempts")
        assert len(attempts["items"]) == 1
        attempt_id = attempts["items"][0]["attempt_id"]
        attempt = await request(api, "GET", f"{path}/attempts/{attempt_id}")
        owner = CONSUMERS[attempt["lease"]["owner"]]
        kernel_id = attempt["runtime"]["session_id"]
        live = await kernels(compose)
        assert len(live) == 1 and live[0]["id"] == kernel_id
        survivor = next(service for service in SERVICES if service != owner)
        if action == "cancel":
            accepted = await request(
                api,
                "POST",
                f"{path}/cancel",
                json={
                    "idempotency_key": f"{unique}-cancel",
                    "actor": ACTOR,
                    "reason": "Docker E2E user cancellation",
                },
            )
            assert accepted["state"]["status"] == "CANCEL_REQUESTED"
        else:
            await stop_owner(compose, owner)

    async with httpx.AsyncClient(base_url=urls[survivor], timeout=10) as api:
        async with asyncio.timeout(90):
            while True:
                detail = await request(api, "GET", path)
                events = await event_history(api, execution_id)
                if (
                    detail["state"]["status"] in {"FAILED", "CANCELLED"}
                    and events[-1]["event_type"] == "execution.completed"
                    and all(
                        e["delivery"]["status"] == "PUBLISHED" for e in events
                    )
                ):
                    break
                await asyncio.sleep(0.5)
        result = await request(api, "GET", f"{path}/result")
        snapshot = await probe(compose, survivor, "snapshot", execution_id)
        validate_evidence(snapshot, result, events, action)
        # Check the same reference across operation, step, and attempt reads.
        for operation in result["operations"]:
            operation_result = await request(
                api,
                "GET",
                f"{path}/operations/{operation['operation_id']}/result",
            )
            assert operation_result["operation"] == operation
            for step in operation["steps"]:
                step_detail = await request(
                    api,
                    "GET",
                    f"{path}/steps/{step['step_id']}",
                )
                assert step_detail["result"] == step["result"]
        attempt = await request(api, "GET", f"{path}/attempts/{attempt_id}")
        assert attempt["lease"]["owner"] is None
        assert attempt["recovery"]["runtime_session_cleanup_status"] == (
            "SUCCEEDED"
        )
        if action == "shutdown":
            assert detail["failure"]["type"] == "WORKER_SHUTDOWN"
        assert await kernels(compose) == [], "Runtime kernel leaked"
        evidence = {
            "action": action,
            "mode": mode,
            "profile": profile,
            "execution_id": execution_id,
            "owner": owner,
            "elapsed_seconds": round(time.monotonic() - start, 2),
            "status": detail["state"]["status"],
            "failure": detail["failure"],
            "notebook_projection": detail["workspace"]["notebook_projection"],
            "events": events,
            "snapshot": snapshot,
            "remaining_kernels": 0,
        }
    if action == "shutdown":
        await compose.run("start", owner)
        port = (
            config.primary_port
            if owner == SERVICES[0]
            else (config.secondary_port)
        )
        await _wait_ready(port)
    return evidence


def checkpoint(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2), encoding="utf-8")
    temporary.replace(path)


async def run(report_path: Path, keep_stack: bool) -> int:
    first, second = available_ports()
    config = Config(
        compose_file=REPOSITORY / "compose.worker-failover.yaml",
        project_name=f"executor-interruption-{uuid4().hex[:12]}",
        primary_port=first,
        secondary_port=second,
        runtime_profile="basic",
        step_duration_seconds=180,
        lease_timeout_seconds=90,
        completion_timeout_seconds=90,
        report_path=report_path,
        jupyter_token=secrets.token_urlsafe(32),
        allow_container_kill=False,
        keep_stack=keep_stack,
        build_jupyter_image=False,
    )
    compose = InterruptionCompose(config)
    report: dict[str, Any] = {
        "project": config.project_name,
        "started_at": datetime.now(UTC).isoformat(),
        "status": "RUNNING",
        "cases": [],
        "git_commit": (
            await asyncio.to_thread(
                subprocess.check_output,
                ["git", "rev-parse", "HEAD"],
                cwd=REPOSITORY,
                text=True,
            )
        ).strip(),
    }
    checkpoint(report_path, report)
    try:
        # UUID scope is generated here, not accepted from user input.
        assert not await compose.run("ps", "--all", "--quiet")
        print(f"Starting isolated project {config.project_name}", flush=True)
        await compose.up()
        await asyncio.gather(_wait_ready(first), _wait_ready(second))
        async with Client(f"http://127.0.0.1:{first}/mcp") as client:
            await upsert_runtime_target(
                client,
                unique=uuid4().hex,
                name="interruption-jupyter",
                endpoint="http://jupyter:8888",
                pool="INTERACTIVE",
                token=config.jupyter_token,
                capacity=2,
            )
        for action in ("cancel", "shutdown"):
            for profile in ("basic", "ml"):
                for mode in ("SINGLE", "MULTI"):
                    report["current_case"] = [action, mode, profile]
                    checkpoint(report_path, report)
                    evidence = await run_case(
                        compose, config, action, mode, profile
                    )
                    report["cases"].append(evidence)
                    checkpoint(report_path, report)
                    print("  PASS", flush=True)
        report["status"] = "PASSED"
    except Exception as error:
        report["status"] = "FAILED"
        report["error"] = f"{type(error).__name__}: {error}"
        report["traceback"] = traceback.format_exc()
        print(report["error"], file=sys.stderr, flush=True)
    finally:
        if not keep_stack:
            await compose.down()
        report["finished_at"] = datetime.now(UTC).isoformat()
        report["stack_retained"] = keep_stack
        checkpoint(report_path, report)
    print(f"Report: {report_path}", flush=True)
    return 0 if report["status"] == "PASSED" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        type=Path,
        default=REPOSITORY / "test-results/docker-interrupted-results.json",
    )
    parser.add_argument("--keep-stack", action="store_true")
    args = parser.parse_args()
    return asyncio.run(run(args.report.resolve(), args.keep_stack))


if __name__ == "__main__":
    raise SystemExit(main())
