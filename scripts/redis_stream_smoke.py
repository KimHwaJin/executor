"""Create a Redis Stream, publish one event, and read it back."""

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from redis.asyncio import Redis

DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/0"
DEFAULT_STREAM_PREFIX = "executor.smoke"


def build_event() -> dict[str, str]:
    execution_id = uuid4()
    return {
        "event_id": str(uuid4()),
        "event_type": "smoke.test",
        "schema_version": "1.0",
        "execution_id": str(execution_id),
        "event_sequence": "1",
        "payload": json.dumps(
            {"message": "Redis Stream smoke test"},
            separators=(",", ":"),
        ),
        "occurred_at": datetime.now(UTC).isoformat(),
    }


async def publish_and_verify(
    redis: Any,
    stream: str,
    event: dict[str, str],
    *,
    allow_existing_stream: bool,
    cleanup: bool,
) -> dict[str, object]:
    if not await redis.ping():
        raise RuntimeError("Redis PING did not return success.")
    existed = bool(await redis.exists(stream))
    if existed and not allow_existing_stream:
        raise RuntimeError(
            "Stream already exists. Use a new name or pass "
            "--allow-existing-stream."
        )

    entry_id = await redis.xadd(stream, event)
    entries = await redis.xrange(stream, min=entry_id, max=entry_id, count=1)
    if len(entries) != 1:
        raise RuntimeError("Published Stream entry could not be read back.")
    read_id, read_event = entries[0]
    if read_id != entry_id or read_event != event:
        raise RuntimeError("Published and read-back Stream entries differ.")

    length = await redis.xlen(stream)
    if cleanup:
        await redis.delete(stream)
    return {
        "status": "PASS",
        "connected": True,
        "stream": stream,
        "stream_existed_before": existed,
        "entry_id": entry_id,
        "event": event,
        "stream_length": length,
        "cleaned_up": cleanup,
    }


def safe_error_message(error: Exception, redis_url: str) -> str:
    message = str(error)
    parsed = urlsplit(redis_url)
    for secret in (parsed.username, parsed.password):
        if secret:
            message = message.replace(secret, "***")
    return message[-500:]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Connect to Redis, create a unique Stream, publish one smoke "
            "event, and verify it by reading the same entry back."
        )
    )
    parser.add_argument(
        "--redis-url",
        default=os.getenv("REDIS_URL", DEFAULT_REDIS_URL),
        help="Redis URL. Defaults to REDIS_URL or local Redis database 0.",
    )
    parser.add_argument(
        "--stream",
        default=None,
        help="Stream name. Defaults to executor.smoke.<random UUID>.",
    )
    parser.add_argument(
        "--allow-existing-stream",
        action="store_true",
        help="Allow appending the smoke event to an existing Stream.",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Delete the tested Stream after successful verification.",
    )
    return parser.parse_args()


async def async_main(args: argparse.Namespace) -> int:
    stream = args.stream or f"{DEFAULT_STREAM_PREFIX}.{uuid4().hex}"
    redis = Redis.from_url(
        args.redis_url,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
    try:
        result = await publish_and_verify(
            redis,
            stream,
            build_event(),
            allow_existing_stream=args.allow_existing_stream,
            cleanup=args.cleanup,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "FAIL",
                    "error_type": type(exc).__name__,
                    "error": safe_error_message(exc, args.redis_url),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        await redis.aclose()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    return asyncio.run(async_main(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
