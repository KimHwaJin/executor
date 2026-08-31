"""Bounded, credential-safe diagnostics for Runtime execution failures.

Never serialize exception dictionaries, locals, request bodies or source lines.
Unknown exception messages can contain SQL parameters or user code: retain their
type and stack locations, not their arbitrary text.
"""

import json
import logging
import os
import re
from datetime import UTC, datetime
from traceback import walk_tb

from executor_service.domain.runtime import (
    ExecutionCompletionError,
    NotebookProjectionInterruptedError,
    RuntimeDriverError,
    RuntimeExecutionError,
    RuntimeExecutionTimeoutError,
    RuntimeOutputLimitExceededError,
)
from executor_service.infrastructure._result_storage.errors import (
    ResultStorageError,
)

_URL = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s<>\"']+")
_SECRET = re.compile(
    r"(?i)(\b(?:token|password|passwd|secret|api[_-]?key|authorization)"
    r"\b[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_AUTH = re.compile(r"(?i)\b(?:bearer|basic|token)\s+[a-z0-9_./+=-]+")


def redact_message(message: str) -> str:
    """Do not expose credential-bearing URLs or common secret assignments."""
    value = _URL.sub("[URL REDACTED]", message)
    value = _AUTH.sub("[AUTH REDACTED]", value)
    value = _SECRET.sub(r"\1[REDACTED]", value)
    return " ".join(value.split())[:2000]


def failure_message(error: BaseException) -> str:
    """Safe operational message; full user-code errors live in result files."""
    if isinstance(
        error,
        (
            RuntimeDriverError,
            ResultStorageError,
            ExecutionCompletionError,
            NotebookProjectionInterruptedError,
        ),
    ):
        return redact_message(str(error))
    if isinstance(error, OSError) and error.errno is not None:
        # filename/filename2 may contain credentials or user data.
        return f"{type(error).__name__}: errno={error.errno} {os.strerror(error.errno)}"
    return f"{type(error).__name__}: execution failed"


def log_runtime_failure(
    logger: logging.Logger,
    error: BaseException,
    *,
    phase: str,
    level: int = logging.ERROR,
    **context: object,
) -> None:
    """Emit searchable context even with the default plain-text log formatter.

    Only pass identifiers/numeric values in context. Exception chains are bounded
    and include stack locations without source snippets or local variable values.
    """
    chain: list[dict[str, object]] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        frames = [
            {
                "file": frame.f_code.co_filename,
                "function": frame.f_code.co_name,
                "line": lineno,
            }
            for frame, lineno in walk_tb(current.__traceback__)
        ][-16:]
        chain.append(
            {
                "type": type(current).__name__,
                "message": (
                    "Code execution failed; inspect Step result files."
                    if isinstance(current, RuntimeExecutionError)
                    and not isinstance(
                        current,
                        (
                            RuntimeExecutionTimeoutError,
                            RuntimeOutputLimitExceededError,
                        ),
                    )
                    else failure_message(current)
                ),
                "stack": frames,
            }
        )
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    payload = {
        "event": "runtime.failure",
        "occurred_at": datetime.now(UTC).isoformat(),
        "phase": phase,
        **{
            key: str(value)
            for key, value in context.items()
            if value is not None
        },
        "errors": chain,
    }
    logger.log(level, "%s", json.dumps(payload, ensure_ascii=True))
