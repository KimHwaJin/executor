"""Bounded Redis 6.0-compatible Stream recovery and retention primitives.

All versions use the same path. Lua keeps inspection and mutation atomic;
PostgreSQL leases, not Redis message ownership, still fence actual execution.
Clients may use either decoded or byte responses.
"""

import logging
from dataclasses import dataclass

from redis.asyncio import Redis

logger = logging.getLogger(__name__)
MAX_BATCH_SIZE = 1000
_MAX_ID_PART = 2**64 - 1
_REQUIRED_COMMANDS = (
    "XPENDING",
    "XCLAIM",
    "XRANGE",
    "XDEL",
    "XINFO",
    "XADD",
    "XACK",
    "XREADGROUP",
    "XGROUP",
    "EVAL",
    "EXISTS",
)

_CLAIM_PENDING = """
local pending = redis.call('XPENDING', KEYS[1], ARGV[1],
                           ARGV[4], '+', ARGV[5])
local ids = {}
local missing = {}
for _, entry in ipairs(pending) do
    if entry[3] >= tonumber(ARGV[3]) then
        local body = redis.call('XRANGE', KEYS[1], entry[1], entry[1])
        if #body == 0 then
            redis.call('XACK', KEYS[1], ARGV[1], entry[1])
            table.insert(missing, entry[1])
        else
            table.insert(ids, entry[1])
        end
    end
end
local messages = {}
if #ids > 0 then
    messages = redis.call('XCLAIM', KEYS[1], ARGV[1], ARGV[2],
                          ARGV[3], unpack(ids))
end
local last = '0-0'
if #pending == tonumber(ARGV[5]) then
    last = pending[#pending][1]
end
return {last, messages, missing}
"""

_TRIM_BEFORE = """
-- Compare ID components as decimal strings, not imprecise Lua doubles.
local function decimal_less(a, b)
    if #a ~= #b then return #a < #b end
    return a < b
end
local function id_less(a, b)
    local am, as = string.match(a, '^(%d+)%-(%d+)$')
    local bm, bs = string.match(b, '^(%d+)%-(%d+)$')
    if am ~= bm then return decimal_less(am, bm) end
    return decimal_less(as, bs)
end
local function field(row, name)
    for i = 1, #row, 2 do
        if row[i] == name then return row[i + 1] end
    end
end
if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
local boundary = ARGV[1]
if ARGV[3] == '1' then
    local groups = redis.call('XINFO', 'GROUPS', KEYS[1])
    if #groups == 0 then return 0 end
    for _, group in ipairs(groups) do
        local delivered = field(group, 'last-delivered-id')
        if not delivered or delivered == '0-0' then return 0 end
        if id_less(delivered, boundary) then boundary = delivered end
        if field(group, 'pending') > 0 then
            local pending = redis.call('XPENDING', KEYS[1],
                                       field(group, 'name'))
            if not pending[2] then return 0 end
            if id_less(pending[2], boundary) then boundary = pending[2] end
        end
    end
end
-- Redis 6.0 does not support exclusive XRANGE bounds. Keep the boundary.
local rows = redis.call('XRANGE', KEYS[1], '-', boundary, 'COUNT', ARGV[2])
local ids = {}
for _, row in ipairs(rows) do
    if id_less(row[1], boundary) then table.insert(ids, row[1]) end
end
if #ids == 0 then return 0 end
return redis.call('XDEL', KEYS[1], unpack(ids))
"""


@dataclass(frozen=True)
class PendingBatch:
    next_cursor: str
    messages: list[tuple[str, dict[str, str]]]
    deleted_ids: list[str]


def _text(value: str | bytes) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _id_parts(value: str) -> tuple[int, int]:
    parts = value.split("-")
    if len(parts) != 2 or any(
        not part.isascii() or not part.isdigit() for part in parts
    ):
        raise ValueError(
            "Stream ID must contain two unsigned decimal integers."
        )
    milliseconds, sequence = map(int, parts)
    if max(milliseconds, sequence) > _MAX_ID_PART:
        raise ValueError("Stream ID component exceeds uint64.")
    return milliseconds, sequence


def next_stream_id(value: str) -> str:
    """Inclusive successor without the Redis 6.2 exclusive-range syntax."""
    milliseconds, sequence = _id_parts(value)
    if sequence < _MAX_ID_PART:
        return f"{milliseconds}-{sequence + 1}"
    if milliseconds < _MAX_ID_PART:
        return f"{milliseconds + 1}-0"
    return "0-0"


async def claim_pending(
    redis: Redis,
    stream: str,
    group: str,
    consumer: str,
    *,
    min_idle_ms: int,
    start_id: str = "0-0",
    count: int = 100,
) -> PendingBatch:
    """Scan at most count PEL entries, including entries not yet claimable."""
    _id_parts(start_id)
    if not 1 <= count <= MAX_BATCH_SIZE or min_idle_ms < 1:
        raise ValueError(
            "Invalid pending recovery batch size or idle timeout."
        )
    result = await redis.execute_command(
        "EVAL",
        _CLAIM_PENDING,
        1,
        stream,
        group,
        consumer,
        min_idle_ms,
        start_id,
        count,
    )
    last, rows, deleted = result
    messages = [
        (
            _text(row[0]),
            {
                _text(key): _text(value)
                for key, value in zip(row[1][::2], row[1][1::2], strict=True)
            },
        )
        for row in rows
    ]
    deleted_ids = [_text(value) for value in deleted]
    if deleted_ids:
        logger.warning(
            "Removed %d pending references whose Stream payload is missing; "
            "stream=%s group=%s message_id_sample=%s",
            len(deleted_ids),
            stream,
            group,
            deleted_ids[:20],
        )
    return PendingBatch(
        next_cursor=next_stream_id(_text(last))
        if _text(last) != "0-0"
        else "0-0",
        messages=messages,
        deleted_ids=deleted_ids,
    )


async def trim_before(
    redis: Redis,
    stream: str,
    boundary: str,
    *,
    protect_groups: bool,
    count: int,
) -> int:
    """Delete a bounded batch, protecting work-group boundaries atomically."""
    milliseconds, sequence = _id_parts(boundary)
    if not 1 <= count <= MAX_BATCH_SIZE:
        raise ValueError("Invalid Stream retention batch size.")
    return int(
        await redis.execute_command(
            "EVAL",
            _TRIM_BEFORE,
            1,
            stream,
            f"{milliseconds}-{sequence}",
            count,
            int(protect_groups),
        )
    )


async def check_redis_compatibility(redis: Redis) -> None:
    """Fail startup clearly rather than endlessly failing background loops."""
    info = await redis.info("server")
    version = _text(info.get("redis_version", info.get(b"redis_version", "")))
    try:
        version_parts = tuple(int(part) for part in version.split(".")[:3])
    except ValueError:
        raise RuntimeError(
            "Unable to determine Redis server version."
        ) from None
    if len(version_parts) != 3 or version_parts < (6, 0, 8):
        raise RuntimeError("Executor requires Redis server 6.0.8 or newer.")
    # COMMAND INFO and EVAL are read-only here (no keys, no writes).
    try:
        commands = await redis.execute_command(
            "COMMAND", "INFO", *_REQUIRED_COMMANDS
        )
    except (TypeError, KeyError, ValueError):
        # redis-py's command parser cannot decode a nil command definition.
        raise RuntimeError(
            "Redis lacks required Executor Stream commands."
        ) from None
    if (
        not isinstance(commands, dict)
        or len(commands) != len(_REQUIRED_COMMANDS)
        or not all(commands.values())
    ):
        raise RuntimeError("Redis lacks required Executor Stream commands.")
    if await redis.execute_command("EVAL", "return 1", 0) != 1:
        raise RuntimeError("Redis scripting is unavailable.")
