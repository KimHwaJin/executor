"""Logging setup: HCP init_logger when installed, else local logger.yml."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from executor_service.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
SETUP = """
from executor_service.logging_setup import configure_logging
from executor_service.settings import Settings
configure_logging(Settings(_env_file=None))
"""


def _run(script: str, path: Path, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=path.parent,
        env={
            **os.environ,
            "APP_ENV": "",
            "LOG_CONFIG_FILE": str(path),
            "LOG_LEVEL": "",
            **env,
        },
        capture_output=True,
        text=True,
        timeout=30,
    )


def _yaml(tmp_path: Path) -> Path:
    path = tmp_path / "logger.yml"
    config = yaml.safe_load((ROOT / "logger.yml").read_text())
    config["formatters"]["standard"]["format"] = "CUSTOM %(message)s"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


def test_local_config_and_root_level_override(tmp_path: Path) -> None:
    result = _run(
        SETUP
        + """
import logging
logging.getLogger('executor_service').debug('visible')
logging.getLogger('httpx').info('hidden')
""",
        _yaml(tmp_path),
        LOG_LEVEL="debug",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["CUSTOM visible"]


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
    path = tmp_path / "bad.yml"
    path.write_text(content, encoding="utf-8")
    result = _run(SETUP, path)
    assert result.returncode != 0
    assert "LoggingConfigurationError" in result.stderr
    assert "unsafe\n" not in result.stdout


def test_missing_file_and_invalid_level(tmp_path: Path) -> None:
    result = _run(SETUP, tmp_path / "missing.yml")
    assert result.returncode != 0
    assert "LOG_CONFIG_FILE" in result.stderr
    result = _run(SETUP, _yaml(tmp_path), LOG_LEVEL="invalid")
    assert result.returncode != 0
    assert "LOG_LEVEL" in result.stderr


def test_settings_preserve_logging_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.delenv("LOG_CONFIG_FILE", raising=False)
    assert Settings(_env_file=None).log_level is None
    assert Settings(_env_file=None).log_config_file == Path("logger.yml")
    monkeypatch.setenv("LOG_LEVEL", " debug ")
    monkeypatch.setenv("LOG_CONFIG_FILE", "/etc/executor/logger.yml")
    settings = Settings(_env_file=None)
    assert settings.log_level == "DEBUG"
    assert settings.log_config_file == Path("/etc/executor/logger.yml")


@pytest.mark.parametrize("internal", [False, True])
def test_entrypoint_uses_local_config_or_hcp_logger(
    tmp_path: Path, internal: bool
) -> None:
    script = """
import logging
import logging.config
import sys
import types
import uvicorn
import executor_service.container as container_module
import executor_service.interfaces.http.app as app_module
from executor_service.infrastructure.db.logging import DatabaseErrorFilter

uvicorn.Config('unused:app')
if USE_INTERNAL:
    # Inside the company network hcp owns logging; no local YAML is read.
    def init_logger():
        logging.config.dictConfig({
            'version': 1, 'disable_existing_loggers': False,
            'formatters': {'custom': {'format': 'INTERNAL %(message)s'}},
            'handlers': {'console': {'class': 'logging.StreamHandler',
                'formatter': 'custom', 'stream': 'ext://sys.stdout'}},
            'root': {'level': 'INFO', 'handlers': ['console']},
            'loggers': {'uvicorn': {'handlers': [], 'propagate': True}},
        })
    hcp_logger = types.ModuleType('hcp.framework.logger')
    hcp_logger.init_logger = init_logger
    sys.modules['hcp.framework.logger'] = hcp_logger

container_module.ApplicationContainer = lambda settings: object()
def create_app(container, *, openapi_tags):
    assert openapi_tags[0]['name'] == 'executions'
    return object()
app_module.create_app = create_app
import executor_service.main as main

class Server:
    def __init__(self, config):
        assert config.log_config is None
        self.config = config
    def run(self):
        self.config.load()
        for handler in logging.getLogger().handlers:
            assert any(isinstance(f, DatabaseErrorFilter) for f in handler.filters)
        for name in ('executor_service.preexisting', 'uvicorn.error', 'uvicorn.access'):
            logging.getLogger(name).info('once')
main.uvicorn.Server = Server
main.run()
""".replace("USE_INTERNAL", str(internal))
    path = tmp_path / "absent.yml" if internal else _yaml(tmp_path)
    result = _run(script, path)
    assert result.returncode == 0, result.stderr
    prefix = "INTERNAL" if internal else "CUSTOM"
    assert result.stdout.splitlines() == [f"{prefix} once"] * 3


def test_image_ships_logging_and_environment_config() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "COPY logger.yml config*.yml ./" in dockerfile
    assert "LOG_CONFIG_FILE=/app/logger.yml" in dockerfile
    deployment = yaml.safe_load(
        (ROOT / "deploy/kubernetes/deployment.yaml").read_text()
    )
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item.get("value") for item in container["env"]}
    assert env["APP_ENV"] == "prd"
