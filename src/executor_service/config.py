"""Local stand-in for the deployment-owned HCP configuration module.

The internal deployment replaces this module with Appconfig(Config),
app_config = Appconfig(), and api_tags_meta. HCP owns configuration and
logging initialization there. No HCP import or emulation is required here.
"""

import logging
import logging.config
from pathlib import Path

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

from executor_service.settings import get_settings


class Appconfig(BaseSettings):
    """Dummy fields; replace with the internal HCP Config subclass."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    FOO: str = ""
    BAR: str = ""


class LoggingConfigurationError(RuntimeError):
    """The configured logging policy could not be applied."""


def _configure_logging(path: Path, level: str | None = None) -> None:
    """Apply dictConfig YAML; an optional LOG_LEVEL overrides only root.level.

    Relative paths are resolved against the process working directory. Missing
    or invalid files fail startup rather than silently losing operator policy.
    Handler factories can execute Python, so this file must be trusted config.
    """
    try:
        with path.open(encoding="utf-8") as handle:
            config = yaml.safe_load(handle)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise LoggingConfigurationError(
            f"Cannot read logging config {path}; check LOG_CONFIG_FILE."
        ) from exc
    if not isinstance(config, dict) or config.get("version") != 1:
        raise LoggingConfigurationError(
            "Logging config must be a dictConfig mapping with version: 1."
        )
    if config.get("incremental"):
        raise LoggingConfigurationError(
            "Startup logging config must not be incremental."
        )
    root = config.get("root")
    if not isinstance(root, dict) or not root.get("handlers"):
        raise LoggingConfigurationError(
            "Logging config must define root.handlers."
        )
    if level is not None:
        normalized = level.upper()
        if normalized not in logging.getLevelNamesMapping():
            raise LoggingConfigurationError("LOG_LEVEL is invalid.")
        root["level"] = normalized
    config.setdefault("disable_existing_loggers", False)
    try:
        logging.config.dictConfig(config)
    except (ValueError, TypeError, AttributeError, ImportError) as exc:
        raise LoggingConfigurationError(
            f"Cannot apply logging config {path}."
        ) from exc


app_config = Appconfig()

# Keep these names when replacing this file in the internal deployment.
# Optional externalDocs entries use {"description": "...", "url": "..."}.
api_tags_meta = [
    {"name": "executions", "description": "Execution requests and results."},
    {"name": "runtime-targets", "description": "Runtime target management."},
    {"name": "maintenance", "description": "Executor maintenance controls."},
]

# Local-only initialization, performed once when main imports this module.
# The internal Config constructor/library supplies its own initialization.
_settings = get_settings()
_configure_logging(_settings.log_config_file, _settings.log_level)
