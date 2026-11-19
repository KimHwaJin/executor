"""Read-only view of the Redis event Streams, for operators inside a Pod.

Connects exactly as Executor does (same settings, Sentinel or standalone)
and only reads: SCAN, XINFO, XREVRANGE, XPENDING and XREAD.

    executor-events streams
    executor-events recent [-t TENANT] [-e EXECUTION_ID] [-n 20] [-v]
    executor-events groups [-t TENANT]
    executor-events follow [-t TENANT] [-e EXECUTION_ID] [-v]

Images without the console script run it from stdin:
    kubectl exec -i POD -- python - recent -t TENANT < event_inspector.py
"""

import argparse
import asyncio
import json
from collections.abc import Iterable

from redis.asyncio import Redis
from redis.typing import KeyT, StreamIdT

from executor_service.domain.tenants import tenant_event_stream
from executor_service.infrastructure.redis_client import create_redis_client
from executor_service.settings import get_settings

type Entry = tuple[str, str, dict[str, str]]  # stream, id, fields

_PAGE = 500


async def _streams(redis: Redis, prefix: str, tenant: str | None) -> list[str]:
    if tenant:
        stream = tenant_event_stream(prefix, tenant)
        return [stream] if await redis.exists(stream) else []
    found = redis.scan_iter(match=f"{prefix}.*", _type="stream", count=_PAGE)
    return sorted([name async for name in found])


def _key(entry_id: str) -> tuple[int, int]:
    ms, seq = entry_id.split("-")
    return int(ms), int(seq)


def _matches(fields: dict[str, str], execution: str | None) -> bool:
    return execution is None or fields.get("execution_id") == execution


def _print(entry: Entry, verbose: bool) -> None:
    stream, entry_id, fields = entry
    if verbose:
        record = {"stream": stream, "id": entry_id, **fields}
        try:
            record["payload"] = json.loads(fields.get("payload", ""))
        except json.JSONDecodeError:
            pass
        print(json.dumps(record, ensure_ascii=False, indent=2))
        return
    try:
        status = json.loads(fields.get("payload", "")).get("status", "")
    except (json.JSONDecodeError, AttributeError):
        status = ""
    print(
        entry_id,
        fields.get("tenant_id", stream),
        fields.get("execution_id", "-"),
        f"#{fields.get('event_sequence', '?')}",
        fields.get("event_type", "-"),
        status,
        fields.get("occurred_at", ""),
    )


async def _list(redis: Redis, streams: Iterable[str]) -> None:
    print("stream", "length", "groups", "first", "last", sep="\t")
    for stream in streams:
        info = await redis.xinfo_stream(stream)
        first, last = info.get("first-entry"), info.get("last-entry")
        print(
            stream,
            info["length"],
            info["groups"],
            first[0] if first else "-",
            last[0] if last else "-",
            sep="\t",
        )


async def _newest(
    redis: Redis, stream: str, count: int, execution: str | None
) -> list[Entry]:
    """Newest matching entries, paging back until count or the start."""

    found: list[Entry] = []
    upper = "+"
    while len(found) < count:
        page = await redis.xrevrange(stream, max=upper, count=_PAGE)
        found += [(stream, i, f) for i, f in page if _matches(f, execution)]
        if len(page) < _PAGE:
            break
        upper = f"({page[-1][0]}"
    return found[:count]


async def _recent(
    redis: Redis,
    streams: Iterable[str],
    count: int,
    execution: str | None,
    verbose: bool,
) -> None:
    entries = [
        entry
        for stream in streams
        for entry in await _newest(redis, stream, count, execution)
    ]
    entries.sort(key=lambda entry: _key(entry[1]))
    for entry in entries[-count:]:
        _print(entry, verbose)


async def _groups(redis: Redis, streams: Iterable[str]) -> None:
    for stream in streams:
        for group in await redis.xinfo_groups(stream):
            name = group["name"]
            print(
                f"{stream} group={name} pending={group['pending']} "
                f"lag={group.get('lag')} "
                f"last-delivered={group['last-delivered-id']}"
            )
            for consumer in await redis.xinfo_consumers(stream, name):
                print(
                    f"  consumer={consumer['name']} "
                    f"pending={consumer['pending']} "
                    f"idle-ms={consumer['idle']}"
                )
            pending = await redis.xpending_range(
                stream, name, min="-", max="+", count=10
            )
            for item in pending:
                print(
                    f"  pending id={item['message_id']} "
                    f"consumer={item['consumer']} "
                    f"idle-ms={item['time_since_delivered']} "
                    f"deliveries={item['times_delivered']}"
                )


async def _follow(
    redis: Redis,
    streams: Iterable[str],
    execution: str | None,
    verbose: bool,
    block_ms: int,
) -> None:
    cursors: dict[KeyT, StreamIdT] = dict.fromkeys(streams, "$")
    while True:
        # Block below the socket timeout so an idle Stream is no error.
        replies = await redis.xread(cursors, block=block_ms)
        for stream, page in replies or []:
            for entry_id, fields in page:
                cursors[stream] = entry_id
                if _matches(fields, execution):
                    _print((stream, entry_id, fields), verbose)


async def _run(args: argparse.Namespace) -> None:
    settings = get_settings()
    redis = create_redis_client(settings)
    try:
        streams = await _streams(
            redis, settings.redis_event_stream, args.tenant
        )
        if not streams:
            print(f"No {settings.redis_event_stream}.* Streams found.")
            return
        match args.command:
            case "streams":
                await _list(redis, streams)
            case "recent":
                await _recent(
                    redis, streams, args.count, args.execution, args.verbose
                )
            case "groups":
                await _groups(redis, streams)
            case "follow":
                block_ms = int(settings.redis_socket_timeout_seconds * 500)
                await _follow(
                    redis, streams, args.execution, args.verbose, block_ms
                )
    finally:
        await redis.aclose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="executor-events", description=__doc__.split("\n\n")[0]
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("streams", "recent", "groups", "follow"):
        command = commands.add_parser(name)
        command.add_argument("-t", "--tenant", help="one tenant's Stream")
        if name in ("recent", "follow"):
            command.add_argument("-e", "--execution", help="execution_id")
            command.add_argument(
                "-v", "--verbose", action="store_true", help="full entries"
            )
        if name == "recent":
            command.add_argument("-n", "--count", type=int, default=20)
    return parser


def run() -> None:
    try:
        asyncio.run(_run(_parser().parse_args()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
