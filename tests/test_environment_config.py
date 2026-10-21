"""Settings layering: environment over .env over config.{APP_ENV}.yml."""

import shutil
from pathlib import Path

import pytest

from executor_service.settings import Settings

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    for name in ("APP_ENV", "DATABASE_POOL_SIZE", "SHARED_STORAGE_ROOT"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_example_file_is_a_valid_prd_config(workdir, monkeypatch):
    shutil.copy(ROOT / "config.example.yml", workdir / "config.prd.yml")
    monkeypatch.setenv("APP_ENV", "prd")
    settings = Settings(_env_file=None)
    assert settings.shared_storage_root == Path("/mnt/data/dtest/executor")
    assert settings.effective_input_storage_root == Path("/mnt/data/dtest")
    assert settings.mcp_allowed_hosts == (
        "executor:*",
        "executor.example.internal:*",
    )


def test_keys_are_case_insensitive_and_env_wins(workdir, monkeypatch):
    (workdir / "config.dev.yml").write_text(
        "database_pool_size: 4\nSHARED_STORAGE_ROOT: /data/executor\n"
        "RUNTIME_CREDENTIAL_KEY: not-the-local-placeholder\n"
    )
    monkeypatch.setenv("APP_ENV", "dev")
    assert Settings(_env_file=None).database_pool_size == 4
    assert Settings(_env_file=None).shared_storage_root == Path(
        "/data/executor"
    )
    monkeypatch.setenv("DATABASE_POOL_SIZE", "7")
    assert Settings(_env_file=None).database_pool_size == 7


def test_local_reads_no_file(workdir, monkeypatch):
    (workdir / "config.local.yml").write_text("database_pool_size: 4\n")
    monkeypatch.setenv("APP_ENV", "local")
    assert Settings(_env_file=None).database_pool_size == 10


def test_unknown_env_and_missing_file_fail(workdir, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(ValueError, match="APP_ENV must be local"):
        Settings(_env_file=None)
    monkeypatch.setenv("APP_ENV", "stg")
    with pytest.raises(FileNotFoundError, match=r"config\.stg\.yml"):
        Settings(_env_file=None)


def test_common_file_applies_and_environment_file_wins(workdir, monkeypatch):
    (workdir / "config.yml").write_text(
        "database_pool_size: 4\nOUTBOX_BATCH_SIZE: 50\n"
    )
    assert Settings(_env_file=None).database_pool_size == 4
    (workdir / "config.dev.yml").write_text(
        "database_pool_size: 6\n"
        "RUNTIME_CREDENTIAL_KEY: not-the-local-placeholder\n"
    )
    monkeypatch.setenv("APP_ENV", "dev")
    settings = Settings(_env_file=None)
    assert (settings.database_pool_size, settings.outbox_batch_size) == (6, 50)
