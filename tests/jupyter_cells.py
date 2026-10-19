"""Run one cell through a real Jupyter Runtime Driver for live tests."""

from typing import Any
from uuid import uuid4

from executor_service.domain.runtime import RuntimeOutputRecord
from executor_service.infrastructure.jupyter import JupyterRuntimeDriver


async def run_cell(
    driver: JupyterRuntimeDriver, session_id: str, code: str
) -> tuple[int | None, list[dict[str, Any]]]:
    """Return the execution count and nbformat outputs of one cell."""

    records: list[RuntimeOutputRecord] = []

    async def collect(record: RuntimeOutputRecord) -> None:
        records.append(record)

    request_id = uuid4()
    await driver.start_execution(session_id, request_id, code)
    result = await driver.follow_execution(session_id, request_id, collect)
    await driver.release_execution(session_id, request_id)
    return result.execution_count, [_notebook_output(r) for r in records]


def _notebook_output(record: RuntimeOutputRecord) -> dict[str, Any]:
    data = {item.media_type: item.content for item in record.representations}
    if record.kind == "STREAM":
        return {
            "output_type": "stream",
            "name": record.stream_name or "stdout",
            "text": data.get("text/plain", ""),
        }
    if record.kind == "ERROR":
        return {
            "output_type": "error",
            "ename": record.metadata.get("ename", "Error"),
            "evalue": record.metadata.get("evalue", ""),
            "traceback": data.get("text/plain", "").splitlines(),
        }
    output: dict[str, Any] = {
        "output_type": (
            "execute_result" if record.kind == "RESULT" else "display_data"
        ),
        "data": data,
        "metadata": dict(record.metadata),
    }
    if record.kind == "RESULT":
        output["execution_count"] = record.execution_count
    return output
