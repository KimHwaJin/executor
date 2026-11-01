"""The caller's X-Request-ID, which links its logs to Executor's."""

import re

from starlette.datastructures import Headers

# The caller chooses this value and it lands in logs.
REQUEST_ID = re.compile(r"[A-Za-z0-9._:\-]{1,128}")


def caller_request_id(headers: Headers) -> str | None:
    value = headers.get("X-Request-ID", "")
    return value if REQUEST_ID.fullmatch(value) else None
