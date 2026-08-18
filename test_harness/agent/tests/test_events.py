"""Redis event boundary filtering tests for the integration Agent."""

import json
from uuid import uuid4

from executor_test_agent.integrations import events as events_module
from executor_test_agent.integrations.events import ExecutionEventWaiter


def _fields(
    execution_id: str,
    event_id: str,
    event_type: str,
    operation_id: str,
) -> dict[str, str]:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "schema_version": "2.0",
        "aggregate_type": "Execution",
        "aggregate_id": execution_id,
        "occurred_at": "2026-08-13T00:00:00Z",
        "payload": json.dumps(
            {
                "schema_version": "2.0",
                "execution_id": execution_id,
                "operation_id": operation_id,
                "status": (
                    "WAITING_FOR_OPERATION"
                    if event_type == "execution.waiting_for_operation"
                    else "SUCCEEDED"
                ),
            }
        ),
    }


async def test_waiter_skips_stale_operation_and_duplicate_event_ids() -> None:
    execution_id = str(uuid4())
    old_operation_id = str(uuid4())
    current_operation_id = str(uuid4())
    duplicate_step_event_id = str(uuid4())
    messages = [
        ("0-0", {"aggregate_id": str(uuid4()), "payload": "not-json"}),
        (
            "1-0",
            _fields(
                execution_id,
                str(uuid4()),
                "execution.waiting_for_operation",
                old_operation_id,
            ),
        ),
        (
            "2-0",
            _fields(
                execution_id,
                duplicate_step_event_id,
                "execution.step_succeeded",
                current_operation_id,
            ),
        ),
        (
            "3-0",
            _fields(
                execution_id,
                duplicate_step_event_id,
                "execution.step_succeeded",
                current_operation_id,
            ),
        ),
        (
            "4-0",
            _fields(
                execution_id,
                str(uuid4()),
                "execution.waiting_for_operation",
                current_operation_id,
            ),
        ),
    ]

    class FakeRedis:
        def __init__(self) -> None:
            self.acked: list[str] = []

        async def xreadgroup(self, **_kwargs):
            return [["executor.events", messages]]

        async def xack(self, _stream, _group, message_id):
            self.acked.append(message_id)

    waiter = ExecutionEventWaiter("redis://unused", "executor.events", "test")
    fake_redis = FakeRedis()
    waiter._redis = fake_redis  # type: ignore[assignment]

    batch = await waiter.wait_for_wakeup(
        execution_id,
        timeout_seconds=1,
        event_types={"execution.waiting_for_operation"},
        operation_id=current_operation_id,
    )

    assert [event.event_type for event in batch.events] == [
        "execution.step_succeeded",
        "execution.waiting_for_operation",
    ]
    assert len({event.event_id for event in batch.events}) == 2
    assert fake_redis.acked == ["0-0", "1-0", "2-0", "3-0", "4-0"]


async def test_event_stream_watermark_returns_latest_id(monkeypatch) -> None:
    class FakeRedis:
        closed = False

        async def xrevrange(self, stream, *, count):
            assert stream == "executor.events"
            assert count == 1
            return [("123-4", {"event_id": "event"})]

        async def aclose(self):
            self.closed = True

    fake = FakeRedis()
    monkeypatch.setattr(events_module.Redis, "from_url", lambda *_args, **_kwargs: fake)

    watermark = await events_module.event_stream_watermark("redis://unused", "executor.events")

    assert watermark == "123-4"
    assert fake.closed is True
