import json
import runpy
import sqlite3
from pathlib import Path
from uuid import uuid4

from executor_service.events import ExecutionStreamEnvelope, build_execution_event

EXAMPLE = runpy.run_path(
    str(Path(__file__).parents[1] / "scripts" / "agent_event_consumer_example.py"),
    run_name="agent_event_consumer_example_test",
)
apply_once = EXAMPLE["_apply_once"]
open_state_database = EXAMPLE["_open_state_database"]


def test_agent_example_applies_one_event_id_exactly_once(tmp_path: Path) -> None:
    execution_id = uuid4()
    event = build_execution_event(
        execution_id=execution_id,
        event_type="execution.succeeded",
        payload={
            "status": "SUCCEEDED",
            "failure_type": None,
            "retry_strategy": "NOT_RETRYABLE",
            "retry_from_sequence": None,
            "runtime_session_cleanup_status": "SUCCEEDED",
        },
    )
    envelope = ExecutionStreamEnvelope.from_redis_fields(
        {
            "event_id": str(event.id),
            "event_type": event.event_type,
            "schema_version": "2.0",
            "aggregate_type": "Execution",
            "aggregate_id": str(execution_id),
            "occurred_at": event.created_at.isoformat(),
            "payload": json.dumps(event.payload),
        }
    )
    database_path = tmp_path / "agent-events.db"
    state = open_state_database(database_path)
    try:
        assert apply_once(state, envelope) is True
        assert apply_once(state, envelope) is False
    finally:
        state.close()

    verification = sqlite3.connect(database_path)
    try:
        consumed_count = verification.execute(
            "SELECT COUNT(*) FROM consumed_executor_events"
        ).fetchone()
        current_state = verification.execute(
            "SELECT event_type, status FROM agent_execution_state WHERE execution_id = ?",
            (str(execution_id),),
        ).fetchone()
    finally:
        verification.close()
    assert consumed_count == (1,)
    assert current_state == ("execution.succeeded", "SUCCEEDED")
