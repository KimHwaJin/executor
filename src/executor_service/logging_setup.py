"""Logging setup that runs with or without the internal HCP library.

Inside the company network ``hcp.framework.logger.init_logger()`` owns
logging and reads its own logger.yml. Outside it, LOG_CONFIG_FILE is
applied with ``logging.dictConfig`` so the same code runs everywhere.
"""

import logging
import logging.config
from pathlib import Path

import yaml

from executor_service.settings import Settings


class LoggingConfigurationError(RuntimeError):
    """The configured logging policy could not be applied."""


def configure_logging(settings: Settings) -> None:
    try:
        from hcp.framework.logger import (  # ty: ignore[unresolved-import]
            init_logger,
        )
    except ImportError:
        apply_logger_config(settings.log_config_file, settings.log_level)
    else:
        init_logger()


def apply_logger_config(path: Path, level: str | None = None) -> None:
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
