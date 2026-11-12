import asyncio
from uuid import uuid4

import pytest

from executor_service.infrastructure.execution_worker.dispatcher import (
    ExecutionJobDispatcher,
)


async def test_failed_job_is_logged_with_its_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    dispatcher = ExecutionJobDispatcher()
    dispatcher.set_accepting(True)
    execution_id = uuid4()

    async def fail() -> None:
        raise RuntimeError("claim failed")

    dispatcher.dispatch(execution_id, fail())
    await dispatcher.wait_idle()
    await asyncio.sleep(0)

    [record] = caplog.records
    assert record.getMessage() == (
        f"Execution job failed execution_id={execution_id} "
        f"job=execution-{execution_id}"
    )
    assert record.exc_info is not None
    assert str(record.exc_info[1]) == "claim failed"
    assert dispatcher.active_job_count == 0


async def test_finished_and_cancelled_jobs_are_not_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    dispatcher = ExecutionJobDispatcher()
    dispatcher.set_accepting(True)

    async def finish() -> None:
        return None

    dispatcher.dispatch(uuid4(), finish())
    dispatcher.dispatch(uuid4(), asyncio.Event().wait())
    await asyncio.sleep(0)
    await dispatcher.cancel_all()
    await dispatcher.wait_idle()

    assert caplog.records == []
