"""Application observability setup."""

from .phoenix import setup_phoenix, shutdown_phoenix

__all__ = ["setup_phoenix", "shutdown_phoenix"]
