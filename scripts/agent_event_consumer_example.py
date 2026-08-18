"""Reference Agent consumer for Executor event contract v2.

The SQLite database is intentionally local to this example. A production Agent should store the
same deduplication key and its own state update in one PostgreSQL transaction before ACKing Redis.
"""

import asyncio
import json
import os
import socket
import sqlite3
from pathlib import Path

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from executor_service.config import get_settings
from executor_service.events import ExecutionStreamEnvelope

TERMINAL_EVENT_TYPES = {
    "execution.succeeded",
    "execution.failed",
    "execution.cancelled",
}


def _open_state_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS consumed_executor_events (
            event_id TEXT PRIMARY KEY,
            execution_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            consumed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_execution_state (
            execution_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            status TEXT,
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.commit()
    return connection


def _apply_once(connection: sqlite3.Connection, event: ExecutionStreamEnvelope) -> bool:
    """Persist Agent state and its deduplication key atomically."""

    event_id = str(event.event_id)
    execution_id = str(event.aggregate_id)
    payload_json = json.dumps(event.payload, separators=(",", ":"), sort_keys=True)
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO consumed_executor_events
                    (event_id, execution_id, event_type, payload)
                VALUES (?, ?, ?, ?)
                """,
                (event_id, execution_id, event.event_type, payload_json),
            )
            connection.execute(
                """
                INSERT INTO agent_execution_state
                    (execution_id, event_type, status, payload, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(execution_id) DO UPDATE SET
                    event_type = excluded.event_type,
                    status = excluded.status,
                    payload = excluded.payload,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    execution_id,
                    event.event_type,
                    event.payload.get("status"),
                    payload_json,
                ),
            )
    except sqlite3.IntegrityError:
        return False
    return True


async def _ensure_group(redis: Redis, stream: str, group: str, start_id: str) -> None:
    try:
        await redis.xgroup_create(stream, group, id=start_id, mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def _consume_message(
    redis: Redis,
    state: sqlite3.Connection,
    *,
    stream: str,
    group: str,
    message_id: str,
    fields: dict[str, str],
    stop_after_terminal: bool,
) -> bool:
    try:
        event = ExecutionStreamEnvelope.from_redis_fields(fields)
    except Exception as exc:
        # Leave invalid messages Pending for operator inspection or a dedicated DLQ.
        print(f"invalid message_id={message_id} error={type(exc).__name__}")
        return False
    applied = _apply_once(state, event)
    await redis.xack(stream, group, message_id)
    print(
        f"event_id={event.event_id} type={event.event_type} "
        f"execution_id={event.aggregate_id} applied={applied}"
    )
    return stop_after_terminal and event.event_type in TERMINAL_EVENT_TYPES


async def main() -> None:
    settings = get_settings()
    stream = os.getenv("AGENT_EVENT_STREAM", settings.redis_event_stream)
    group = os.getenv("AGENT_EVENT_CONSUMER_GROUP", "agent-execution-events")
    group_start_id = os.getenv("AGENT_EVENT_GROUP_START_ID", "$")
    consumer = os.getenv(
        "AGENT_EVENT_CONSUMER_NAME", f"agent-example-{socket.gethostname()}-{os.getpid()}"
    )
    state_path = Path(os.getenv("AGENT_EVENT_STATE_DB", ".agent-event-consumer.db"))
    pending_idle_milliseconds = int(os.getenv("AGENT_EVENT_PENDING_IDLE_MILLISECONDS", "30000"))
    stop_after_terminal = os.getenv("AGENT_EVENT_STOP_AFTER_TERMINAL", "false").lower() in {
        "1",
        "true",
        "yes",
    }
    redis = Redis.from_url(settings.redis_dsn, decode_responses=True)
    state = _open_state_database(state_path)
    try:
        await _ensure_group(redis, stream, group, group_start_id)
        print(f"consuming stream={stream} group={group} consumer={consumer}")
        pending_claim_cursor = "0-0"
        while True:
            claimed = await redis.xautoclaim(
                stream,
                group,
                consumer,
                min_idle_time=pending_idle_milliseconds,
                start_id=pending_claim_cursor,
                count=20,
            )
            pending_claim_cursor = str(claimed[0])
            for message_id, fields in claimed[1]:
                if await _consume_message(
                    redis,
                    state,
                    stream=stream,
                    group=group,
                    message_id=message_id,
                    fields=fields,
                    stop_after_terminal=stop_after_terminal,
                ):
                    return
            batches = await redis.xreadgroup(
                groupname=group,
                consumername=consumer,
                streams={stream: ">"},
                count=20,
                block=1000,
            )
            for _, messages in batches:
                for message_id, fields in messages:
                    if await _consume_message(
                        redis,
                        state,
                        stream=stream,
                        group=group,
                        message_id=message_id,
                        fields=fields,
                        stop_after_terminal=stop_after_terminal,
                    ):
                        return
    finally:
        state.close()
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
