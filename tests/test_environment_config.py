"""Settings layering: environment over .env over config.{APP_ENV}.yml."""

import shutil
from pathlib import Path

import pytest

from executor_service import settings as settings_module
from executor_service.settings import APP_ENVS, Settings

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    for name in ("APP_ENV", "DATABASE_POOL_SIZE", "SHARED_STORAGE_ROOT"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


@pytest.mark.parametrize("app_env", APP_ENVS)
def test_shipped_files_are_valid(workdir, monkeypatch, app_env):
    for name in ("config.yml", f"config.{app_env}.yml"):
        shutil.copy(ROOT / name, workdir / name)
    monkeypatch.setenv("APP_ENV", app_env)
    settings = Settings(_env_file=None)
    assert settings.db_auto_migrate is True
    assert settings.shared_storage_root == Path("/mnt/data/dtest/executor")
    assert settings.effective_input_storage_root == Path("/mnt/data/dtest")
    assert settings.mcp_allowed_hosts[0] == "executor:*"


def test_local_values_match_settings_defaults(monkeypatch):
    """config.yml is read locally too, so it must not change local behaviour."""
    monkeypatch.chdir(ROOT)
    monkeypatch.delenv("APP_ENV", raising=False)
    from_files = Settings(_env_file=None)
    monkeypatch.setattr(settings_module, "config_files", list)
    assert from_files == Settings(_env_file=None)


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


@pytest.mark.parametrize("app_env", [None, ""])
def test_local_reads_only_common_file(workdir, monkeypatch, app_env):
    (workdir / "config.yml").write_text("database_pool_size: 4\n")
    (workdir / "config.dev.yml").write_text("database_pool_size: 6\n")
    if app_env is not None:
        monkeypatch.setenv("APP_ENV", app_env)
    assert Settings(_env_file=None).database_pool_size == 4


def test_app_env_comes_from_environment_only(workdir, monkeypatch):
    (workdir / ".env").write_text("APP_ENV=prd\n")
    assert Settings().database_pool_size == 10


def test_deployed_env_rejects_placeholder_credential_key(workdir, monkeypatch):
    (workdir / "config.prd.yml").write_text("database_pool_size: 4\n")
    monkeypatch.setenv("APP_ENV", "prd")
    with pytest.raises(ValueError, match="RUNTIME_CREDENTIAL_KEY"):
        Settings(_env_file=None)


@pytest.mark.parametrize("app_env", ["local", "production", "PRD", "../dev"])
def test_unknown_env_fails(workdir, monkeypatch, app_env):
    monkeypatch.setenv("APP_ENV", app_env)
    with pytest.raises(ValueError, match="APP_ENV must be one of"):
        Settings(_env_file=None)


def test_missing_environment_file_fails(workdir, monkeypatch):
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


def test_port_comes_from_config_yml(workdir, monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    (workdir / "config.yml").write_text("PORT: 9300\n")
    assert Settings(_env_file=None).port == 9300
