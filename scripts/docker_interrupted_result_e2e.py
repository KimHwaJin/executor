"""Isolated cancel / SIGTERM / SIGKILL / stale-Worker result E2E.

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
NOTEBOOK_READ_PROBE = """
import json, os, stat, sys, tempfile
from pathlib import Path
import nbformat
from nbconvert import HTMLExporter
root = Path(os.environ['JUPYTER_ROOT_DIR']).resolve()
path = (root / sys.argv[1]).resolve()
assert path.is_relative_to(root)
mode = stat.S_IMODE(path.stat().st_mode)
assert mode == 0o644, oct(mode)
assert os.getuid() == 65534
with tempfile.TemporaryDirectory() as home:
    os.environ['HOME'] = home
    notebook = nbformat.read(path, as_version=4)
    nbformat.validate(notebook)
    html, _ = HTMLExporter().from_notebook_node(notebook)
    assert 'completed-before-interrupt' in html
print(json.dumps({'mode': oct(mode), 'reader_uid': os.getuid(),
                  'cell_count': len(notebook.cells), 'rendered': True}))
"""


class InterruptionCompose(Compose):
    async def up(self) -> None:
        await self.run("build", "migrate", "jupyter")
        await self.run("up", "--detach")

    def _command(self, *arguments: str) -> list[str]:
        return super()._command(
            "--file",
            str(REPOSITORY / "compose.interrupted-results.yaml"),
            *arguments,
        )

    async def down(self) -> None:
        # Do not report successful cleanup if Docker rejected the operation.
        await self.run(
            "down", "--volumes", "--remove-orphans", "--timeout", "45"
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
        context={"user_id": ACTOR["id"], "task_id": unique},
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


async def interrupt_owner(compose: Compose, service: str, action: str) -> None:
    if service not in SERVICES or action not in {"kill", "pause"}:
        raise ValueError("Refusing to interrupt an unmanaged service/action")
    if action == "kill":
        await compose.kill(service)
    else:
        await compose.run("pause", service)


async def run_case(
    compose: Compose,
    config: Config,
    action: str,
    mode: str,
    profile: str,
    evidence: dict[str, Any],
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
        evidence["execution_id"] = execution_id
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
        elif action == "shutdown":
            await stop_owner(compose, owner)
        else:
            await interrupt_owner(compose, owner, action)

    async with httpx.AsyncClient(base_url=urls[survivor], timeout=10) as api:
        async with asyncio.timeout(90):
            while True:
                detail = await request(api, "GET", path)
                events = await event_history(api, execution_id)
                if (
                    detail["state"]["status"] in {"FAILED", "CANCELLED"}
                    and detail["recovery"]["runtime_session_cleanup_status"]
                    == "SUCCEEDED"
                    and events
                    and events[-1]["event_type"] == "execution.completed"
                    and all(
                        e["delivery"]["status"] == "PUBLISHED" for e in events
                    )
                ):
                    break
                await asyncio.sleep(0.5)
        result = await request(api, "GET", f"{path}/result")
        snapshot = await probe(compose, survivor, "snapshot", execution_id)
        evidence.update(
            detail=detail, result=result, snapshot=snapshot, events=events
        )
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
        if action in {"shutdown", "kill", "pause"}:
            assert detail["failure"]["type"] == (
                "WORKER_SHUTDOWN" if action == "shutdown" else "LEASE_EXPIRED"
            )
            assert attempt["recovery"]["retry_strategy"] == (
                "FROM_START" if mode == "SINGLE" else "NOT_RETRYABLE"
            )
        histories = await request(
            api, "GET", f"{path}/attempts/{attempt_id}/steps"
        )
        logical = {
            step["step_id"]: step
            for op in result["operations"]
            for step in op["steps"]
        }
        assert not histories["has_more"]
        assert {h["sequence"] for h in histories["items"]} == {0, 1}
        for history in histories["items"]:
            current = logical[history["execution_step_id"]]
            assert (
                history["result"]["result_ref"]
                == (current["result"]["result_ref"])
            )
        projection = detail["workspace"]["notebook_projection"]
        assert projection["status"] == "FAILED", (
            f"Notebook falsely current after {action}: {projection}"
        )
        assert "may be stale" in projection["error_message"]
        assert projection["projected_at"] is None
        diagnostics = await request(api, "GET", f"{path}/diagnostics")
        assert any(
            item["diagnostic"]["phase"]
            == (
                "NOTEBOOK_LEASE_EXPIRED"
                if action in {"kill", "pause"}
                else "NOTEBOOK_INTERRUPTED"
            )
            for item in diagnostics["items"]
        )
        notebook = await request(api, "GET", f"{path}/notebook")
        assert notebook["page"]["total_count"] == 3
        assert notebook["cells"][0]["output_summary"]["output_count"] == 1
        # Partial output is authoritative in shared results, not yet projected
        # into this notebook. The API must report that explicitly above.
        assert notebook["cells"][1]["output_summary"]["output_count"] == 0
        shared_read = json.loads(
            await compose.run(
                "exec",
                "-T",
                "--user",
                "65534:65534",
                "jupyter",
                "python",
                "-B",
                "-c",
                NOTEBOOK_READ_PROBE,
                detail["workspace"]["notebook_path"],
            )
        )
        assert await kernels(compose) == [], "Runtime kernel leaked"
        evidence.update(
            {
                "action": action,
                "mode": mode,
                "profile": profile,
                "execution_id": execution_id,
                "owner": owner,
                "elapsed_seconds": round(time.monotonic() - start, 2),
                "status": detail["state"]["status"],
                "failure": detail["failure"],
                "notebook_projection": detail["workspace"][
                    "notebook_projection"
                ],
                "events": events,
                "snapshot": snapshot,
                "diagnostics": diagnostics,
                "notebook": notebook,
                "notebook_shared_read": shared_read,
                "remaining_kernels": 0,
            }
        )
        if action == "pause":
            await compose.run("unpause", owner)
            port = (
                config.primary_port
                if owner == SERVICES[0]
                else config.secondary_port
            )
            await _wait_ready(port)
            # Observe beyond two heartbeats: old coroutines must not attach
            # their late seal, change state or publish an additional event.
            for _ in range(24):
                await asyncio.sleep(0.5)
                assert await request(api, "GET", f"{path}/result") == result
                current_events = await event_history(api, execution_id)
                assert current_events == events
            assert (
                await probe(compose, survivor, "snapshot", execution_id)
                == snapshot
            )
            assert await kernels(compose) == []
            evidence["stale_worker_observed_seconds"] = 12
    if action in {"shutdown", "kill"}:
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


async def run(
    report_path: Path,
    keep_stack: bool,
    actions: tuple[str, ...] = ("cancel", "shutdown"),
    profiles: tuple[str, ...] = ("basic", "ml"),
    modes: tuple[str, ...] = ("SINGLE", "MULTI"),
) -> int:
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
        "ports": {"primary": first, "secondary": second},
        "git_commit": (
            await asyncio.to_thread(
                subprocess.check_output,
                ["git", "rev-parse", "HEAD"],
                cwd=REPOSITORY,
                text=True,
            )
        ).strip(),
    }
    owned = False
    checkpoint(report_path, report)
    try:
        # UUID scope is generated here, not accepted from user input.
        assert not await compose.run("ps", "--all", "--quiet")
        owned = True
        print(f"Starting isolated project {config.project_name}", flush=True)
        await compose.up()
        await asyncio.gather(_wait_ready(first), _wait_ready(second))
        # Linux identity regression: writer 1000, reader nobody, initial and
        # repeated writes, including a pre-existing 0600 notebook. Core HTML
        # rendering is tested, not a deployed nbviewer HTTP service.
        await compose.run(
            "exec",
            "-T",
            "--user",
            "0:0",
            "jupyter",
            "python",
            "-B",
            "/opt/tests/notebook_shared_read.py",
        )
        report["notebook_644_identity_test"] = "PASSED"
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
        for action in actions:
            for profile in profiles:
                for mode in modes:
                    report["current_case"] = [action, mode, profile]
                    checkpoint(report_path, report)
                    evidence: dict[str, Any] = {
                        "action": action,
                        "mode": mode,
                        "profile": profile,
                    }
                    report["cases"].append(evidence)
                    await run_case(
                        compose, config, action, mode, profile, evidence
                    )
                    checkpoint(report_path, report)
                    print("  PASS", flush=True)
        report["status"] = "PASSED"
    except Exception as error:
        report["status"] = "FAILED"
        report["error"] = f"{type(error).__name__}: {error}"
        report["traceback"] = traceback.format_exc()
        print(report["error"], file=sys.stderr, flush=True)
    finally:
        report["stack_retained"] = owned
        if owned and not keep_stack:
            try:
                # A failed assertion may leave one owned test Worker frozen.
                for service in SERVICES:
                    await compose.run(
                        "unpause", service, tolerate_failure=True
                    )
                await compose.down()
                report["stack_retained"] = False
            except Exception as error:
                report["status"] = "FAILED"
                report["cleanup_error"] = f"{type(error).__name__}: {error}"
        report["finished_at"] = datetime.now(UTC).isoformat()
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
    parser.add_argument(
        "--actions",
        nargs="+",
        choices=("cancel", "shutdown", "kill", "pause"),
        default=["cancel", "shutdown"],
    )
    parser.add_argument(
        "--profiles",
        nargs="+",
        choices=("basic", "ml"),
        default=["basic", "ml"],
    )
    parser.add_argument(
        "--modes",
        nargs="+",
        choices=("SINGLE", "MULTI"),
        default=["SINGLE", "MULTI"],
    )
    args = parser.parse_args()
    return asyncio.run(
        run(
            args.report.resolve(),
            args.keep_stack,
            tuple(args.actions),
            tuple(args.profiles),
            tuple(args.modes),
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
