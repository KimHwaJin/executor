import asyncio
import json

import pytest

from executor_service.infrastructure.jupyter import execution


async def test_small_frames_parse_inline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail(*_args: object) -> object:
        raise AssertionError("small frames should not use a thread")

    monkeypatch.setattr(asyncio, "to_thread", fail)

    assert await execution._decode('{"seq": 1}') == {"seq": 1}


async def test_large_frames_parse_in_a_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    original = asyncio.to_thread

    async def recording(func, raw):
        calls.append(len(raw))
        return await original(func, raw)

    monkeypatch.setattr(asyncio, "to_thread", recording)
    raw = json.dumps({"text": "x" * execution._LARGE_MESSAGE_BYTES})

    assert (await execution._decode(raw))["text"].startswith("x")
    assert calls == [len(raw)]
