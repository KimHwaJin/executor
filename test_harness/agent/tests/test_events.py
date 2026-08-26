"""Redis event boundary filtering tests for the integration Agent."""

import json
from uuid import uuid4

import pytest

from executor_test_agent.integrations import events as events_module
from executor_test_agent.integrations.events import ExecutionEventWaiter


def _fields(
    execution_id: str,
    event_id: str,
    event_type: str,
    operation_id: str,
    event_sequence: int,
) -> dict[str, str]:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "schema_version": "1.0",
        "execution_id": execution_id,
        "event_sequence": str(event_sequence),
        "occurred_at": "2026-08-13T00:00:00Z",
        "payload": json.dumps(
            {
                "operation": {"id": operation_id, "number": 1},
                "status": (
                    "WAITING_FOR_OPERATION"
                    if event_type == "execution.operation_completed"
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
        ("0-0", {"execution_id": str(uuid4()), "payload": "not-json"}),
        (
            "1-0",
            _fields(
                execution_id,
                str(uuid4()),
                "execution.operation_completed",
                old_operation_id,
                1,
            ),
        ),
        (
            "2-0",
            _fields(
                execution_id,
                duplicate_step_event_id,
                "execution.step_completed",
                current_operation_id,
                2,
            ),
        ),
        (
            "3-0",
            _fields(
                execution_id,
                duplicate_step_event_id,
                "execution.step_completed",
                current_operation_id,
                2,
            ),
        ),
        (
            "4-0",
            _fields(
                execution_id,
                str(uuid4()),
                "execution.operation_completed",
                current_operation_id,
                3,
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
        event_types={"execution.operation_completed"},
        operation_id=current_operation_id,
    )

    assert [event.event_type for event in batch.events] == [
        "execution.step_completed",
        "execution.operation_completed",
    ]
    assert len({event.event_id for event in batch.events}) == 2
    assert fake_redis.acked == ["0-0", "1-0", "2-0", "3-0", "4-0"]


async def test_waiter_recovers_a_sequence_gap_from_event_history() -> None:
    execution_id = str(uuid4())
    operation_id = str(uuid4())
    first = _fields(
        execution_id,
        str(uuid4()),
        "execution.step_started",
        operation_id,
        1,
    )
    second = _fields(
        execution_id,
        str(uuid4()),
        "execution.step_completed",
        operation_id,
        2,
    )
    terminal = _fields(
        execution_id,
        str(uuid4()),
        "execution.operation_completed",
        operation_id,
        3,
    )

    class FakeRedis:
        async def xreadgroup(self, **_kwargs):
            return [["executor.events", [("3-0", terminal)]]]

        async def xack(self, *_args):
            return 1

    async def load_history(
        requested_execution_id: str, after_sequence: int, limit: int
    ) -> list[dict]:
        assert requested_execution_id == execution_id
        assert after_sequence == 0
        assert limit == 2
        return [
            {
                **fields,
                "event_sequence": int(fields["event_sequence"]),
                "payload": json.loads(fields["payload"]),
            }
            for fields in (first, second)
        ]

    waiter = ExecutionEventWaiter(
        "redis://unused",
        "executor.events",
        "test",
        history_loader=load_history,
    )
    waiter._redis = FakeRedis()  # type: ignore[assignment]

    batch = await waiter.wait_for_wakeup(
        execution_id,
        timeout_seconds=1,
        event_types={"execution.operation_completed"},
        operation_id=operation_id,
    )

    assert [event.event_sequence for event in batch.events] == [1, 2, 3]
    assert batch.wake_event.event_sequence == 3


async def test_waiter_does_not_ack_a_later_event_when_gap_recovery_fails() -> None:
    execution_id = str(uuid4())
    operation_id = str(uuid4())
    terminal = _fields(
        execution_id,
        str(uuid4()),
        "execution.operation_completed",
        operation_id,
        2,
    )

    class FakeRedis:
        def __init__(self) -> None:
            self.acked: list[str] = []

        async def xreadgroup(self, **_kwargs):
            return [["executor.events", [("2-0", terminal)]]]

        async def xack(self, _stream, _group, message_id):
            self.acked.append(message_id)

    async def load_history(_execution_id: str, _after_sequence: int, _limit: int) -> list[dict]:
        return []

    waiter = ExecutionEventWaiter(
        "redis://unused",
        "executor.events",
        "test",
        history_loader=load_history,
    )
    fake_redis = FakeRedis()
    waiter._redis = fake_redis  # type: ignore[assignment]

    with pytest.raises(ValueError, match="could not be recovered"):
        await waiter.wait_for_wakeup(
            execution_id,
            timeout_seconds=1,
            event_types={"execution.operation_completed"},
            operation_id=operation_id,
        )

    assert fake_redis.acked == []


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
