"""config.example.yml lists every setting, at its code default."""

from pathlib import Path

import yaml

from executor_service.settings import Settings

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yml"

# Shown with a deployment value instead of the code default.
PER_ENVIRONMENT = {
    "AUTH_CLIENTS",
    "DATABASE_URL",
    "DB_AUTO_MIGRATE",
    "INPUT_STORAGE_ROOT",
    "MCP_ALLOWED_HOSTS",
    "MCP_ALLOWED_ORIGINS",
    "REDIS_URL",
    "RUNTIME_CREDENTIAL_KEY",
    "SHARED_STORAGE_ROOT",
}


def load_example() -> dict:
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def test_lists_every_setting():
    assert set(load_example()) == {
        name.upper() for name in Settings.model_fields
    }


def test_is_valid_and_shared_values_equal_defaults(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("APP_ENV", raising=False)
    example = Settings(_env_file=None, **load_example())
    default = Settings(_env_file=None)
    for name in Settings.model_fields:
        if name.upper() not in PER_ENVIRONMENT:
            assert getattr(example, name) == getattr(default, name), name
