"""Errors raised by shared execution result storage."""


class ResultStorageError(RuntimeError):
    """Shared result storage could not safely persist or read a result."""
