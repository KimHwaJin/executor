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
        "RUNTIME_ALLOWED_PROFILES=basic,ml\n"
        "DATABASE_POOL_SIZE=12\n"
        "DATABASE_MAX_OVERFLOW=3\n"
        "DATABASE_POOL_TIMEOUT_SECONDS=7.5\n"
        "DATABASE_POOL_RECYCLE_SECONDS=900\n"
        "DATABASE_CONNECT_TIMEOUT_SECONDS=4\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=dotenv)

    assert settings.mcp_allowed_hosts == (
        "localhost:*",
        "127.0.0.1:*",
        "testserver",
    )
    assert settings.mcp_allowed_origins == (
        "http://localhost:*",
        "http://127.0.0.1:*",
    )
    assert settings.runtime_allowed_profiles == ("basic", "ml")
    assert settings.database_pool_size == 12
    assert settings.database_max_overflow == 3
    assert settings.database_pool_timeout_seconds == 7.5
    assert settings.database_pool_recycle_seconds == 900
    assert settings.database_connect_timeout_seconds == 4


def test_non_local_environment_rejects_placeholder_secrets() -> None:
    with pytest.raises(ValueError, match="RUNTIME_CREDENTIAL_KEY"):
        Settings(app_env="production")


def test_settings_have_no_bootstrap_runtime_target_fields() -> None:
    settings = Settings()

    assert not hasattr(settings, "runtime_target_name")
    assert not hasattr(settings, "jupyter_endpoint")
    assert not hasattr(settings, "jupyter_token")
    assert not hasattr(settings, "runtime_pool")


def test_windows_async_runner_uses_selector_event_loop() -> None:
    async def current_loop() -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()

    loop = run_async(current_loop(), platform="win32")

    assert isinstance(loop, asyncio.SelectorEventLoop)
