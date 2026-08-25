"""Tests for event-driven Executor reconciliation."""

from types import SimpleNamespace
from typing import cast

from executor_test_agent.config import AgentSettings
from executor_test_agent.integrations import workflow


async def test_reconciliation_retries_after_mcp_task_group_failure(
    monkeypatch,
) -> None:
    sleeps: list[float] = []
    offloaded_calls: list[tuple[object, ...]] = []

    class FakeClient:
        def __init__(self, _url: str) -> None:
            self.active = False
            self.fail_on_close = not clients
            clients.append(self)

        async def __aenter__(self) -> "FakeClient":
            self.active = True
            return self

        async def __aexit__(self, *_args: object) -> None:
            self.active = False
            if self.fail_on_close:
                raise ExceptionGroup(
                    "MCP transport failed",
                    [ConnectionError("stream closed")],
                )

    clients: list[FakeClient] = []

    async def fake_fetch(client: FakeClient, execution_id: str) -> dict:
        assert client.active
        assert execution_id == "execution-1"
        return {"operations": []}

    async def fake_fetch_detail(client: FakeClient, execution_id: str) -> dict:
        assert client.active
        assert execution_id == "execution-1"
        return {"runtime": {}, "workspace": {}}

    def fake_resolve(result: dict, _root: object) -> dict:
        assert all(not client.active for client in clients)
        return result

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def fake_to_thread(function, *args: object) -> dict:
        offloaded_calls.append((function, *args))
        return function(*args)

    monkeypatch.setattr(workflow, "Client", FakeClient)
    monkeypatch.setattr(workflow, "fetch_execution_result", fake_fetch)
    monkeypatch.setattr(workflow, "fetch_execution_detail", fake_fetch_detail)
    monkeypatch.setattr(workflow, "resolve_execution_result", fake_resolve)
    monkeypatch.setattr(workflow.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(workflow.asyncio, "to_thread", fake_to_thread)
    settings = cast(
        AgentSettings,
        SimpleNamespace(
            executor_mcp_url="http://executor.test/mcp",
            executor_shared_storage_root="unused",
        ),
    )

    result = await workflow._reconciled_result("execution-1", settings)

    assert result == (
        {"operations": []},
        {"runtime": {}, "workspace": {}},
    )
    assert len(clients) == 2
    assert sleeps == [workflow.RESULT_RECONCILIATION_RETRY_SECONDS]
    assert offloaded_calls == [(fake_resolve, {"operations": []}, "unused")]
