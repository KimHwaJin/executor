"""Verify that timed-out Jupyter code is interrupted and the kernel is idle."""

import asyncio
import os
from uuid import uuid4

from executor_service.domain.enums import RuntimeAbortStatus
from executor_service.infrastructure.jupyter import JupyterRuntimeDriver
from executor_service.settings import get_settings


async def _expect_timeout(
    driver: JupyterRuntimeDriver,
    session_id: str,
    code: str,
    timeout_seconds: float,
) -> None:
    try:
        async with asyncio.timeout(timeout_seconds):
            await driver.execute(session_id, code)
    except TimeoutError:
        return
    raise RuntimeError("Jupyter code unexpectedly completed before timeout.")


async def main() -> None:
    endpoint = os.getenv("JUPYTER_GATEWAY_ENDPOINT")
    token = os.getenv("JUPYTER_GATEWAY_TOKEN")
    if not endpoint or not token:
        raise RuntimeError(
            "JUPYTER_GATEWAY_ENDPOINT and JUPYTER_GATEWAY_TOKEN are required."
        )
    settings = get_settings()
    workspace = (
        "users/timeout-smoke/projects/unscoped/sessions/unscoped/"
        f"executions/{uuid4()}"
    )
    marker_path = f"{workspace}/artifacts/other/delayed-marker.txt"
    driver = JupyterRuntimeDriver(
        endpoint,
        token,
        settings.jupyter_request_timeout_seconds,
        settings.jupyter_storage_timeout_seconds,
    )
    session_id: str | None = None
    try:
        await driver.prepare_workspace(workspace)
        session_id = await driver.start_session("default", workspace)

        await _expect_timeout(
            driver,
            session_id,
            (
                "import time\n"
                "from pathlib import Path\n"
                "time.sleep(3)\n"
                "Path('artifacts/other/delayed-marker.txt').write_text("
                "'should-not-exist', encoding='utf-8')"
            ),
            0.5,
        )
        delayed_abort = await driver.abort_session(session_id, 5)
        if delayed_abort.status != RuntimeAbortStatus.IDLE_CONFIRMED:
            raise RuntimeError(
                "Delayed-marker Runtime did not confirm idle after abort: "
                f"{delayed_abort}"
            )
        await asyncio.sleep(3)
        snapshot = await driver.artifact_snapshot(workspace)
        if any(file.path == marker_path for file in snapshot.files):
            raise RuntimeError(
                "Timed-out code continued and wrote the delayed marker."
            )

        await _expect_timeout(
            driver,
            session_id,
            "while True:\n    pass",
            0.5,
        )
        loop_abort = await driver.abort_session(session_id, 5)
        if loop_abort.status != RuntimeAbortStatus.IDLE_CONFIRMED:
            raise RuntimeError(
                "Infinite-loop Runtime did not reach a bounded idle state: "
                f"{loop_abort}"
            )
        result = await driver.execute(session_id, "print('responsive')")
        if not any(
            output.get("output_type") == "stream"
            and "responsive" in str(output.get("text", ""))
            for output in result.outputs
        ):
            raise RuntimeError(
                "Kernel did not execute code after idle confirmation."
            )

        print("workspace:", workspace)
        print("delayed_marker_written: false")
        print("infinite_loop_abort: IDLE_CONFIRMED")
        print("kernel_responsive: true")
    finally:
        if session_id is not None:
            await driver.delete_session(session_id)
        await driver.close()


if __name__ == "__main__":
    asyncio.run(main())
