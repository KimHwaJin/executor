import asyncio

import pytest

from executor_service import main


def test_windows_serves_on_selector_loop(monkeypatch: pytest.MonkeyPatch):
    loops = []

    class Server:
        def __init__(self, config):
            pass

        async def serve(self):
            loops.append(type(asyncio.get_running_loop()))

    monkeypatch.setattr(main.sys, "platform", "win32")
    monkeypatch.setattr(main.uvicorn, "Server", Server)
    main.run()
    assert loops == [asyncio.SelectorEventLoop]
