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


async def test_run_turned_away_by_an_ending_job_asks_for_a_retry() -> None:
    retries = 0

    def retry() -> None:
        nonlocal retries
        retries += 1

    dispatcher = ExecutionJobDispatcher(on_retry=retry)
    dispatcher.set_accepting(True)
    execution_id = uuid4()
    ending = asyncio.Event()

    async def next_run() -> None:
        return None

    dispatcher.dispatch(execution_id, ending.wait())
    dispatcher.dispatch(uuid4(), next_run())
    await asyncio.sleep(0)
    dispatcher.dispatch(execution_id, next_run())
    assert retries == 0

    ending.set()
    await dispatcher.wait_idle()

    assert retries == 1
