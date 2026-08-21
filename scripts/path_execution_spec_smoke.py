"""Verify PATH input storage is separate from Jupyter-owned execution storage."""

import asyncio
import hashlib
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from execution_spec_payload import execution_request
from mcp import Client


async def _required(client: Client, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    if result.is_error or result.structured_content is None:
        raise RuntimeError(f"{tool} failed: {result.content}")
    return result.structured_content


async def _wait_terminal(client: Client, execution_id: str) -> dict[str, Any]:
    for _ in range(300):
        execution = await _required(client, "execution_get", {"execution_id": execution_id})
        if execution["state"]["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return execution
        await asyncio.sleep(0.2)
    raise RuntimeError(f"Execution {execution_id} did not finish.")


def _publish_input(unique: str) -> tuple[Path, Path, Path, bytes]:
    input_root = Path(os.getenv("EXECUTOR_INPUT_HOST_ROOT", "input_dir")).resolve()
    relative_path = Path("smoke") / unique / "step-0.py"
    source_path = input_root / relative_path
    temporary_path = source_path.with_suffix(".tmp")
    content = (
        b"from pathlib import Path\n"
        b"Path('artifacts/other/path-source.txt').write_text("
        b"'runtime-owned', encoding='utf-8')\n"
        b"print('PATH source executed')"
    )
    source_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path.write_bytes(content)
    temporary_path.replace(source_path)
    return input_root, relative_path, source_path, content


def _cleanup_input(source_path: Path) -> None:
    source_path.with_suffix(".tmp").unlink(missing_ok=True)
    source_path.unlink(missing_ok=True)
    try:
        source_path.parent.rmdir()
        source_path.parent.parent.rmdir()
    except OSError:
        pass


async def main() -> None:
    unique = uuid4().hex
    input_root, relative_path, source_path, content = await asyncio.to_thread(
        _publish_input, unique
    )

    try:
        async with Client(os.getenv("EXECUTOR_MCP_URL", "http://127.0.0.1:8000/mcp")) as client:
            submitted = await _required(
                client,
                "execution_submit",
                {
                    "request": execution_request(
                        idempotency_key=f"path-smoke-submit-{unique}",
                        operation_mode="SINGLE",
                        trigger_type="INTERACTIVE",
                        runtime_profile="basic",
                        spec={
                            "schema_version": "1.0",
                            "steps": [
                                {
                                    "sequence": 0,
                                    "payload": {
                                        "type": "PYTHON_EXECUTE",
                                        "source": {
                                            "type": "PATH",
                                            "path": relative_path.as_posix(),
                                            "sha256": hashlib.sha256(content).hexdigest(),
                                        },
                                    },
                                    "lineage": {
                                        "skill_name": "report",
                                        "tool_name": "path_source_probe",
                                        "input_parameters": {},
                                    },
                                }
                            ],
                        },
                        context={
                            "user_id": "path-smoke-user",
                            "project_id": "path-smoke-project",
                            "session_id": f"path-smoke-session-{unique}",
                            "task_id": f"path-smoke-task-{unique}",
                        },
                        actor={"type": "USER", "id": "path-smoke-user"},
                    )
                },
            )
            execution_id = str(submitted["execution_id"])
            terminal = await _wait_terminal(client, execution_id)
            if terminal["state"]["status"] != "SUCCEEDED":
                raise RuntimeError(f"PATH Execution failed: {terminal}")
            notebook = await _required(
                client,
                "execution_notebook_read",
                {"execution_id": execution_id, "response_format": "detailed", "limit": 0},
            )
            artifacts = await _required(
                client, "execution_artifact_list", {"execution_id": execution_id, "limit": 100}
            )
        source_exists = await asyncio.to_thread(source_path.is_file)
        notebooks = await asyncio.to_thread(lambda: list(input_root.rglob("*.ipynb")))
        if not source_exists:
            raise RuntimeError("Executor modified the immutable PATH input file.")
        if notebooks:
            raise RuntimeError("A Runtime notebook leaked into Agent/Executor input storage.")
        names = {item["name"] for item in artifacts["items"]}
        if notebook["page"]["total_count"] != 1 or not {
            "execution.ipynb",
            "path-source.txt",
        }.issubset(names):
            raise RuntimeError("Runtime outputs were not materialized on Jupyter storage.")
    finally:
        await asyncio.to_thread(_cleanup_input, source_path)

    print("execution_id:", execution_id)
    print("input_source:", relative_path.as_posix())
    print("notebook_path:", terminal["workspace"]["notebook_path"])
    print("artifacts:", sorted(names))


if __name__ == "__main__":
    asyncio.run(main())
