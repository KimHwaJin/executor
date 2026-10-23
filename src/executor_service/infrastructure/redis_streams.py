"""Bounded Redis 6.0-compatible Stream retention.

Lua keeps inspection and deletion atomic. Clients may use either decoded or
byte responses.
"""

from redis.asyncio import Redis

MAX_BATCH_SIZE = 1000
_MAX_ID_PART = 2**64 - 1
_REQUIRED_COMMANDS = ("XADD", "XRANGE", "XDEL", "EVAL", "EXISTS")

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
if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
local boundary = ARGV[1]
-- Redis 6.0 does not support exclusive XRANGE bounds. Keep the boundary.
local rows = redis.call('XRANGE', KEYS[1], '-', boundary, 'COUNT', ARGV[2])
local ids = {}
for _, row in ipairs(rows) do
    if id_less(row[1], boundary) then table.insert(ids, row[1]) end
end
if #ids == 0 then return 0 end
return redis.call('XDEL', KEYS[1], unpack(ids))
"""


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


async def trim_before(
    redis: Redis,
    stream: str,
    boundary: str,
    *,
    count: int,
) -> int:
    """Delete a bounded batch of entries older than boundary."""
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
