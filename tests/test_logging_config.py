"""Logging policy tests, with real handlers isolated from pytest's logging."""

import logging.config
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from executor_service.config import Settings
from executor_service.logging_config import (
    LoggingConfigurationError,
    configure_logging,
)

ROOT = Path(__file__).resolve().parents[1]


def test_yaml_and_root_level_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "logger.yml"
    config = yaml.safe_load((ROOT / "logger.yml").read_text())
    config["formatters"]["standard"]["format"] = "CUSTOM %(message)s"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    captured: list[dict] = []
    monkeypatch.setattr(logging.config, "dictConfig", captured.append)
    configure_logging(path, "debug")
    assert captured[0]["root"]["level"] == "DEBUG"
    assert captured[0]["loggers"]["httpx"]["level"] == "WARNING"
    assert (
        captured[0]["formatters"]["standard"]["format"] == "CUSTOM %(message)s"
    )
    configure_logging(path)
    assert captured[1]["root"]["level"] == "INFO"


@pytest.mark.parametrize(
    "content",
    [
        "[invalid",
        "[]",
        "version: 2",
        "version: 1\nroot: {}",
        "version: 1\nincremental: true",
        "version: 1\nroot:\n  handlers: [missing]",
        "!!python/object/apply:builtins.print ['unsafe']",
    ],
)
def test_invalid_yaml_fails_startup(tmp_path: Path, content: str) -> None:
    # Do not run dictConfig failure cases in the pytest logging process.
    path = tmp_path / "bad.yml"
    path.write_text(content, encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; "
            "from executor_service.logging_config import configure_logging; "
            f"configure_logging(Path({str(path)!r}))",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode != 0
    assert "LoggingConfigurationError" in result.stderr
    assert "unsafe\n" not in result.stdout


def test_missing_file_and_invalid_level(tmp_path: Path) -> None:
    with pytest.raises(LoggingConfigurationError, match="LOG_CONFIG_FILE"):
        configure_logging(tmp_path / "missing.yml")
    with pytest.raises(LoggingConfigurationError, match="LOG_LEVEL"):
        configure_logging(ROOT / "logger.yml", "not-a-level")


def test_settings_support_yaml_default_and_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.delenv("LOG_CONFIG_FILE", raising=False)
    assert Settings(_env_file=None).log_level is None
    assert Settings(_env_file=None).log_config_file == Path("logger.yml")
    monkeypatch.setenv("LOG_LEVEL", " debug ")
    monkeypatch.setenv("LOG_CONFIG_FILE", "C:/executor/logger.yml")
    settings = Settings(_env_file=None)
    assert settings.log_level == "DEBUG"
    assert settings.log_config_file == Path("C:/executor/logger.yml")
    monkeypatch.setenv("LOG_LEVEL", "")
    assert Settings(_env_file=None).log_level is None


def test_entrypoint_keeps_uvicorn_and_module_logs_unified(
    tmp_path: Path,
) -> None:
    path = tmp_path / "logger.yml"
    config = yaml.safe_load((ROOT / "logger.yml").read_text())
    config["formatters"]["standard"]["format"] = (
        "CUSTOM %(levelname)s %(name)s %(message)s"
    )
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    script = """
import logging
from pathlib import Path
import uvicorn
import executor_service.config as settings_module
import executor_service.container as container_module
import executor_service.interfaces.http.app as app_module

# No database/Redis connection or port binding is needed for startup logging.
settings_module.get_settings = lambda: settings_module.Settings(
    _env_file=None, log_config_file=Path(CONFIG_PATH), log_level=None
)
container_module.ApplicationContainer = lambda settings: object()
app_module.create_app = lambda container: object()
logging.getLogger("executor_service.preexisting")
uvicorn.Config("unused:app")  # Reproduce previously configured Uvicorn handlers.
import executor_service.main as main

class Server:
    def __init__(self, config):
        assert config.log_config is None
    def run(self):
        for name in (
            "executor_service.preexisting", "uvicorn.error", "uvicorn.access"
        ):
            logging.getLogger(name).info("once")
        logging.getLogger("httpx").info("hidden")
        logging.getLogger("httpx").warning("visible")

main.uvicorn.Server = Server
# Exercise entrypoint's normal synchronous server path on every test platform.
main.sys.platform = "linux"
main.run()
""".replace("CONFIG_PATH", repr(str(path)))
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "CUSTOM INFO executor_service.preexisting once",
        "CUSTOM INFO uvicorn.error once",
        "CUSTOM INFO uvicorn.access once",
        "CUSTOM WARNING httpx visible",
    ]


def test_image_and_deployment_logging_paths_match() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "COPY logger.yml ./" in dockerfile
    assert "LOG_CONFIG_FILE=/app/logger.yml" in dockerfile
    configmap = yaml.safe_load(
        (ROOT / "deploy/kubernetes/configmap.yaml").read_text()
    )
    assert configmap["data"]["LOG_CONFIG_FILE"] == "/app/logger.yml"
