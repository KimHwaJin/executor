import asyncio
from pathlib import Path

import pytest

from executor_service.config import Settings
from executor_service.event_loop import run_async


def test_comma_separated_lists_load_from_dotenv(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "MCP_ALLOWED_HOSTS=localhost:*,127.0.0.1:*,testserver\n"
        "MCP_ALLOWED_ORIGINS=http://localhost:*,http://127.0.0.1:*\n"
        "RUNTIME_ALLOWED_PROFILES=python3,python-analysis\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=dotenv)

    assert settings.mcp_allowed_hosts == ("localhost:*", "127.0.0.1:*", "testserver")
    assert settings.mcp_allowed_origins == (
        "http://localhost:*",
        "http://127.0.0.1:*",
    )
    assert settings.runtime_allowed_profiles == ("python3", "python-analysis")


def test_non_local_environment_rejects_placeholder_secrets() -> None:
    with pytest.raises(ValueError, match="JUPYTER_TOKEN"):
        Settings(app_env="production")


def test_windows_async_runner_uses_selector_event_loop() -> None:
    async def current_loop() -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()

    loop = run_async(current_loop(), platform="win32")

    assert isinstance(loop, asyncio.SelectorEventLoop)
