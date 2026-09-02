"""Verify INLINE/PATH Python Steps and INLINE/PATH Markdown report materialization."""

import asyncio
import hashlib
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from execution_spec_payload import execution_request
from local_test_support import executor_mcp_url, required_tool_result
from mcp import Client

TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED"}


async def _wait_terminal(client: Client, execution_id: str) -> dict[str, Any]:
    for _ in range(600):
        execution = await required_tool_result(
            client, "execution_get", {"execution_id": execution_id}
        )
        if execution["state"]["status"] in TERMINAL_STATUSES:
            return execution
        await asyncio.sleep(0.2)
    raise TimeoutError(
        f"Execution {execution_id} did not reach a terminal state."
    )


def _step(source: dict[str, str]) -> dict[str, Any]:
    return {
        "sequence": 0,
        "payload": {"type": "PYTHON_EXECUTE", "source": source},
        "lineage": {
            "skill_name": "report",
            "tool_name": "source_report_matrix",
            "input_parameters": {},
        },
    }


async def _submit(
    client: Client,
    *,
    unique: str,
    label: str,
    source: dict[str, str],
) -> str:
    submitted = await required_tool_result(
        client,
        "execution_submit",
        {
            "request": execution_request(
                idempotency_key=f"source-report-{label}-submit-{unique}",
                operation_mode="SINGLE",
                trigger_type="INTERACTIVE",
                actor={"type": "USER", "id": "source-report-user"},
                runtime_profile="default",
                spec={"schema_version": "1.0", "steps": [_step(source)]},
                context={
                    "user_id": "source-report-user",
                    "project_id": "source-report-project",
                    "session_id": f"source-report-{label}-session-{unique}",
                    "task_id": f"source-report-{label}-task-{unique}",
                },
            )
        },
    )
    execution_id = str(submitted["execution_id"])
    terminal = await _wait_terminal(client, execution_id)
    if terminal["state"]["status"] != "SUCCEEDED":
        raise RuntimeError(f"{label} Python execution failed: {terminal}")
    return execution_id


async def _create_report(
    client: Client,
    *,
    unique: str,
    label: str,
    execution_id: str,
    source: dict[str, str],
) -> dict[str, Any]:
    artifact = await required_tool_result(
        client,
        "execution_artifact_create",
        {
            "request": {
                "execution_id": execution_id,
                "idempotency_key": f"source-report-{label}-artifact-{unique}",
                "type": "REPORT",
                "source": source,
                "name": f"{label}-report.md",
                "description": f"{label} Markdown report smoke",
                "media_type": "text/markdown",
                "append_to_notebook": True,
                "metadata": {
                    "test_run_id": unique,
                    "source_type": label.upper(),
                },
                "actor": {"type": "USER", "id": "source-report-user"},
            }
        },
    )
    if artifact["status"] != "AVAILABLE":
        raise RuntimeError(f"{label} report is not AVAILABLE: {artifact}")
    if artifact["storage"]["media_type"] != "text/markdown":
        raise RuntimeError(
            f"{label} report media type is incorrect: {artifact}"
        )
    if not artifact["storage"]["relative_path"].endswith(
        f"/reports/{label}-report.md"
    ):
        raise RuntimeError(f"{label} report path is incorrect: {artifact}")
    return artifact


async def _assert_notebook(
    client: Client, execution_id: str, python_marker: str, report_marker: str
) -> None:
    notebook = await required_tool_result(
        client,
        "execution_notebook_read",
        {
            "execution_id": execution_id,
            "view": "FULL",
            "start_index": 0,
            "limit": 20,
        },
    )
    cells = notebook["cells"]
    if len(cells) != 2:
        raise RuntimeError(f"Expected one code and one Markdown cell: {cells}")
    if cells[0]["type"] != "code" or python_marker not in cells[0]["source"]:
        raise RuntimeError(
            f"Python source was not persisted correctly: {cells[0]}"
        )
    if (
        cells[1]["type"] != "markdown"
        or report_marker not in cells[1]["source"]
    ):
        raise RuntimeError(
            f"Markdown report was not appended correctly: {cells[1]}"
        )


def _prepare_inputs(
    unique: str, python_content: str, report_content: str
) -> tuple[Path, Path, Path]:
    shared_root = Path(
        os.getenv("LOCAL_TEST_SHARED_STORAGE_ROOT", "shared_dir")
    ).resolve()
    input_root = shared_root / "requests"
    relative_root = Path("smoke") / f"source-report-{unique}"
    source_dir = input_root / relative_root
    python_path = source_dir / "step.py"
    report_path = source_dir / "report.md"
    source_dir.mkdir(parents=True, exist_ok=False)
    python_path.write_text(python_content, encoding="utf-8")
    report_path.write_text(report_content, encoding="utf-8")
    return relative_root, python_path, report_path


def _cleanup_inputs(python_path: Path, report_path: Path) -> None:
    python_path.unlink(missing_ok=True)
    report_path.unlink(missing_ok=True)
    try:
        python_path.parent.rmdir()
        python_path.parent.parent.rmdir()
    except OSError:
        pass


async def main() -> None:
    unique = uuid4().hex
    inline_python_marker = f"INLINE_PYTHON:{unique}"
    path_python_marker = f"PATH_PYTHON:{unique}"
    inline_report_marker = f"INLINE_MARKDOWN:{unique}"
    path_report_marker = f"PATH_MARKDOWN:{unique}"
    python_content = f"print({path_python_marker!r})\n"
    report_content = f"# Path report\n\n{path_report_marker}\n"
    relative_root, python_path, report_path = await asyncio.to_thread(
        _prepare_inputs, unique, python_content, report_content
    )

    try:
        async with Client(executor_mcp_url()) as client:
            inline_execution_id = await _submit(
                client,
                unique=unique,
                label="inline",
                source={
                    "type": "INLINE",
                    "content": f"print({inline_python_marker!r})",
                },
            )
            inline_artifact = await _create_report(
                client,
                unique=unique,
                label="inline",
                execution_id=inline_execution_id,
                source={
                    "type": "INLINE",
                    "content": f"# Inline report\n\n{inline_report_marker}\n",
                },
            )
            await _assert_notebook(
                client,
                inline_execution_id,
                inline_python_marker,
                inline_report_marker,
            )

            path_execution_id = await _submit(
                client,
                unique=unique,
                label="path",
                source={
                    "type": "PATH",
                    "path": (relative_root / python_path.name).as_posix(),
                    "sha256": hashlib.sha256(
                        python_content.encode()
                    ).hexdigest(),
                },
            )
            path_artifact = await _create_report(
                client,
                unique=unique,
                label="path",
                execution_id=path_execution_id,
                source={
                    "type": "PATH",
                    "path": (relative_root / report_path.name).as_posix(),
                    "sha256": hashlib.sha256(
                        report_content.encode()
                    ).hexdigest(),
                },
            )
            await _assert_notebook(
                client,
                path_execution_id,
                path_python_marker,
                path_report_marker,
            )
    finally:
        await asyncio.to_thread(_cleanup_inputs, python_path, report_path)

    print("status: PASSED")
    print("inline_execution_id:", inline_execution_id)
    print("inline_report_path:", inline_artifact["storage"]["relative_path"])
    print("path_execution_id:", path_execution_id)
    print("path_report_path:", path_artifact["storage"]["relative_path"])


if __name__ == "__main__":
    asyncio.run(main())
