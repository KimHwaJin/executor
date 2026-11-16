"""REDIS_MODE=sentinel wiring, without a Sentinel deployment."""

import sys
from types import ModuleType
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from executor_service.infrastructure.redis_client import create_redis_client
from executor_service.settings import Settings


def _sentinel_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "redis_mode": "sentinel",
        "redis_sentinel_host": "sentinel.test",
        "redis_sentinel_port": 26380,
        "redis_sentinel_master_name": "mymaster",
        "redis_sentinel_username": "executor",
        "redis_sentinel_auth": "encrypted",
        "redis_connect_timeout_seconds": 2,
        "redis_socket_timeout_seconds": 3,
    }
    return Settings(_env_file=None, **(values | overrides))


@pytest.fixture
def hcp_cipher(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for the internal library, absent outside the company."""

    class AESCipher:
        def decrypt_str(self, value: str) -> str:
            return f"plain:{value}"

    module = ModuleType("hcp.framework.util.crypto_cipher")
    module.AESCipher = AESCipher  # type: ignore[attr-defined]
    for name in ("hcp", "hcp.framework", "hcp.framework.util"):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    monkeypatch.setitem(sys.modules, module.__name__, module)


@pytest.mark.parametrize(
    "missing",
    [
        "redis_sentinel_host",
        "redis_sentinel_master_name",
        "redis_sentinel_auth",
    ],
)
def test_sentinel_mode_requires_its_connection_values(missing: str) -> None:
    with pytest.raises(ValidationError, match="REDIS_MODE=sentinel"):
        _sentinel_settings(**{missing: None})


def test_standalone_is_the_default_and_needs_no_sentinel_values() -> None:
    assert Settings(_env_file=None).redis_mode == "standalone"


async def test_sentinel_client_follows_the_master_with_one_account(
    hcp_cipher: None,
) -> None:
    client = create_redis_client(_sentinel_settings())
    pool = client.connection_pool
    try:
        assert pool.service_name == "mymaster"
        assert pool.is_master is True
        [sentinel] = pool.sentinel_manager.sentinels
        sentinel_options = sentinel.connection_pool.connection_kwargs
        assert (sentinel_options["host"], sentinel_options["port"]) == (
            "sentinel.test",
            26380,
        )
        for options in (pool.connection_kwargs, sentinel_options):
            assert options["username"] == "executor"
            assert options["password"] == "plain:encrypted"
            assert options["socket_connect_timeout"] == 2
            assert options["socket_timeout"] == 3
            assert options["decode_responses"] is True
            # The Outbox owns retries.
            assert options["retry"]._retries == 0
        assert "db" not in pool.connection_kwargs
    finally:
        await client.aclose()


async def test_closing_the_client_closes_sentinel_connections(
    hcp_cipher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = create_redis_client(_sentinel_settings())
    [sentinel] = client.connection_pool.sentinel_manager.sentinels
    closed = AsyncMock()
    monkeypatch.setattr(sentinel, "aclose", closed)

    await client.aclose()

    closed.assert_awaited_once()


def test_sentinel_mode_fails_clearly_without_the_internal_library(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "hcp", None)
    with pytest.raises(RuntimeError, match="hcp"):
        create_redis_client(_sentinel_settings())
