"""Safe diagnostics for nested asynchronous integration failures."""

import re

_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s]+@")


def exception_summary(exc: BaseException, *, max_length: int = 500) -> str:
    """Render leaf failures instead of hiding them behind ExceptionGroup."""

    leaves = _exception_leaves(exc)
    summaries: list[str] = []
    for leaf in leaves:
        message = " ".join(str(leaf).split())
        message = _URL_CREDENTIALS.sub(r"\g<scheme>***@", message)
        summary = type(leaf).__name__
        if message:
            summary = f"{summary}: {message}"
        if summary not in summaries:
            summaries.append(summary)
    rendered = "; ".join(summaries) or type(exc).__name__
    if len(rendered) <= max_length:
        return rendered
    return f"{rendered[: max_length - 3]}..."


def _exception_leaves(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for child in exc.exceptions for leaf in _exception_leaves(child)]
    cause = exc.__cause__
    if cause is not None:
        return _exception_leaves(cause)
    return [exc]
