import asyncio
from pathlib import Path

from executor_service.config import Settings
from executor_service.event_loop import run_async


def test_comma_separated_mcp_allowlists_load_from_dotenv(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "MCP_ALLOWED_HOSTS=localhost:*,127.0.0.1:*,testserver\n"
        "MCP_ALLOWED_ORIGINS=http://localhost:*,http://127.0.0.1:*\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=dotenv)

    assert settings.mcp_allowed_hosts == ("localhost:*", "127.0.0.1:*", "testserver")
    assert settings.mcp_allowed_origins == (
        "http://localhost:*",
        "http://127.0.0.1:*",
    )


def test_json_mcp_allowlists_remain_compatible(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "MCP_ALLOWED_HOSTS='[\"localhost:*\",\"testserver\"]'\n"
        "MCP_ALLOWED_ORIGINS='[\"http://localhost:*\"]'\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=dotenv)

    assert settings.mcp_allowed_hosts == ("localhost:*", "testserver")
    assert settings.mcp_allowed_origins == ("http://localhost:*",)


def test_windows_async_runner_uses_selector_event_loop() -> None:
    async def current_loop() -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()

    loop = run_async(current_loop(), platform="win32")

    assert isinstance(loop, asyncio.SelectorEventLoop)
