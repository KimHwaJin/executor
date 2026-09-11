import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from executor_service.domain.enums import FailureType, RetryStrategy
from executor_service.domain.runtime import (
    RuntimeDriverError,
    RuntimeExecutionError,
    RuntimeSessionLostError,
)
from executor_service.infrastructure._jupyter.execution_guard import (
    KernelExecutionGuard,
)
from executor_service.infrastructure._jupyter.transport import (
    JupyterHttpTransport,
)
from executor_service.infrastructure.execution_worker.failure_policy import (
    failure_policy,
)
from executor_service.infrastructure.execution_worker.types import (
    StoredStepFailure,
)
from executor_service.infrastructure.jupyter import JupyterRuntimeDriver


def state(instance: str = "original", *, alive: bool = True) -> httpx.Response:
    return httpx.Response(
        200,
        json={"kernel_id": "kernel", "alive": alive, "instance_id": instance},
    )


@pytest.fixture
async def transport() -> AsyncIterator[JupyterHttpTransport]:
    value = JupyterHttpTransport("http://jupyter.invalid", "secret", 1, 1)
    await value.client.aclose()
    value.client = httpx.AsyncClient(
        base_url="http://jupyter.invalid",
        transport=httpx.MockTransport(lambda _: state()),
    )
    try:
        yield value
    finally:
        await value.close()


async def install(
    transport: JupyterHttpTransport,
    handler: Callable[[httpx.Request], Any],
) -> KernelExecutionGuard:
    await transport.client.aclose()
    transport.client = httpx.AsyncClient(
        base_url="http://jupyter.invalid",
        transport=httpx.MockTransport(handler),
    )
    return KernelExecutionGuard(
        transport,
        poll_seconds=0.005,
        probe_timeout_seconds=0.02,
        failure_threshold=3,
    )


@pytest.mark.parametrize("second", [state(alive=False), state("replacement")])
async def test_loss_stops_wait_even_with_live_websocket(
    transport: JupyterHttpTransport, second: httpx.Response
) -> None:
    responses = iter([state(), second])
    guard = await install(transport, lambda _: next(responses))
    stopped = asyncio.Event()

    async def blocked() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async with asyncio.timeout(1):
        with pytest.raises(RuntimeSessionLostError) as caught:
            await guard.run("kernel", blocked)
    assert stopped.is_set()
    assert "unknown" in str(caught.value)
    assert failure_policy(StoredStepFailure(caught.value, None), True) == (
        FailureType.RUNTIME_SESSION_LOST,
        RetryStrategy.FROM_START,
    )


@pytest.mark.parametrize("status", [404, 401, 503])
async def test_missing_extension_never_sends_code(
    transport: JupyterHttpTransport, status: int
) -> None:
    guard = await install(transport, lambda _: httpx.Response(status))
    called = False

    async def execute() -> None:
        nonlocal called
        called = True

    with pytest.raises(RuntimeDriverError):
        await guard.run("kernel", execute)
    assert not called


async def test_silent_computation_and_temporary_probe_failures_are_allowed(
    transport: JupyterHttpTransport,
) -> None:
    calls = 0

    def response(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        # Two failures, recovery, then two failures again: consecutive only.
        return httpx.Response(503) if calls in {2, 3, 5, 6} else state()

    guard = await install(transport, response)

    async def silent() -> str:
        await asyncio.sleep(0.06)
        return "done"

    assert await guard.run("kernel", silent) == "done"
    assert calls >= 7
    at_completion = calls
    await asyncio.sleep(0.02)
    assert calls == at_completion


async def test_repeated_probe_failure_is_unavailable_not_claimed_dead(
    transport: JupyterHttpTransport,
) -> None:
    calls = 0

    async def response(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls > 1:
            await asyncio.sleep(1)
        return state()

    guard = await install(transport, response)

    async def blocked() -> None:
        await asyncio.Event().wait()

    async with asyncio.timeout(1):
        with pytest.raises(
            RuntimeDriverError, match="not confirmed"
        ) as caught:
            await guard.run("kernel", blocked)
    assert calls == 4
    assert not isinstance(caught.value, RuntimeSessionLostError)


@pytest.mark.parametrize("code_error", [False, True])
async def test_restart_at_reply_boundary_is_not_success_or_reusable_error(
    transport: JupyterHttpTransport,
    code_error: bool,
) -> None:
    responses = iter([state(), state(alive=False)])
    guard = await install(transport, lambda _: next(responses))

    async def reply() -> str:
        if code_error:
            raise RuntimeExecutionError("KeyboardInterrupt", outputs=[])
        return "done"

    with pytest.raises(RuntimeSessionLostError):
        await guard.run("kernel", reply)


async def test_normal_code_error_is_preserved_when_kernel_is_unchanged(
    transport: JupyterHttpTransport,
) -> None:
    guard = await install(transport, lambda _: state())
    original = RuntimeExecutionError("ValueError", outputs=[])

    async def reply() -> None:
        raise original

    with pytest.raises(RuntimeExecutionError) as caught:
        await guard.run("kernel", reply)
    assert caught.value is original


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"kernel_id": "wrong"},
        {
            "kernel_id": "kernel",
            "alive": "false",
            "instance_id": "same",
        },
        {"kernel_id": "kernel", "alive": True, "instance_id": None},
    ],
)
async def test_invalid_observation_does_not_look_alive(
    transport: JupyterHttpTransport,
    payload: object,
) -> None:
    guard = await install(
        transport, lambda _: httpx.Response(200, json=payload)
    )

    async def reply() -> None:
        raise AssertionError("Code must not be sent")

    with pytest.raises(RuntimeDriverError):
        await guard.run("kernel", reply)


async def test_cancellation_drains_both_tasks(
    transport: JupyterHttpTransport,
) -> None:
    calls = 0
    stopped = asyncio.Event()

    def response(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return state()

    guard = await install(transport, response)

    async def blocked() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    task = asyncio.create_task(guard.run("kernel", blocked))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
    after = calls
    await asyncio.sleep(0.02)
    assert calls == after


@pytest.mark.parametrize("status", ["restarting", "dead"])
async def test_unparented_server_notification_is_not_discarded(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    class Socket:
        async def __aenter__(self) -> "Socket":
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def send(self, _: bytes) -> None:
            pass

        async def recv(self) -> str:
            return json.dumps(
                {
                    "channel": "iopub",
                    "header": {"msg_type": "status"},
                    "parent_header": {},
                    "content": {"execution_state": status},
                }
            )

    monkeypatch.setattr(
        "executor_service.infrastructure._jupyter.execution.websockets.connect",
        lambda *_args, **_kwargs: Socket(),
    )
    driver = JupyterRuntimeDriver("http://jupyter.invalid", "secret")
    await driver._client.aclose()
    driver._client = httpx.AsyncClient(
        base_url="http://jupyter.invalid",
        transport=httpx.MockTransport(lambda _: state()),
    )
    try:
        with pytest.raises(RuntimeSessionLostError, match=status):
            await driver.execute("kernel", "pass")
    finally:
        await driver.close()
