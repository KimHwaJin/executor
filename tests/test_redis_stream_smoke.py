import runpy
from pathlib import Path
from typing import Any

import pytest

SCRIPT = runpy.run_path(
    str(Path(__file__).parents[1] / "scripts" / "redis_stream_smoke.py"),
    run_name="redis_stream_smoke_test",
)
build_event = SCRIPT["build_event"]
publish_and_verify = SCRIPT["publish_and_verify"]
safe_error_message = SCRIPT["safe_error_message"]


class FakeRedis:
    def __init__(self, *, existed: bool = False) -> None:
        self.existed = existed
        self.entries: dict[str, dict[str, str]] = {}
        self.deleted: list[str] = []

    async def ping(self) -> bool:
        return True

    async def exists(self, stream: str) -> int:
        return int(self.existed)

    async def xadd(self, stream: str, event: dict[str, str]) -> str:
        self.entries["1-0"] = event
        return "1-0"

    async def xrange(self, *args: Any, **kwargs: Any) -> list[Any]:
        return list(self.entries.items())

    async def xlen(self, stream: str) -> int:
        return len(self.entries)

    async def delete(self, stream: str) -> int:
        self.deleted.append(stream)
        return 1


@pytest.mark.asyncio
async def test_smoke_event_is_published_read_back_and_cleaned_up() -> None:
    redis = FakeRedis()
    event = build_event()
    result = await publish_and_verify(
        redis,
        "executor.smoke.test",
        event,
        allow_existing_stream=False,
        cleanup=True,
    )

    assert result["status"] == "PASS"
    assert result["event"] == event
    assert result["entry_id"] == "1-0"
    assert result["stream_length"] == 1
    assert result["cleaned_up"] is True
    assert redis.deleted == ["executor.smoke.test"]
    assert set(event) == {
        "event_id",
        "event_type",
        "schema_version",
        "execution_id",
        "event_sequence",
        "payload",
        "occurred_at",
    }


@pytest.mark.asyncio
async def test_existing_stream_requires_explicit_permission() -> None:
    with pytest.raises(RuntimeError, match="already exists"):
        await publish_and_verify(
            FakeRedis(existed=True),
            "executor.events",
            build_event(),
            allow_existing_stream=False,
            cleanup=False,
        )


def test_error_message_redacts_url_credentials() -> None:
    url = "redis://user:secret@redis.internal:6379/0"
    message = safe_error_message(
        RuntimeError(f"failed for {url} user secret"),
        url,
    )
    assert "user" not in message
    assert "secret" not in message
