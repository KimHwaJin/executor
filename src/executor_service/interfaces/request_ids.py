"""The caller's X-Request-ID, kept only when it is safe to log."""

import re

from starlette.datastructures import Headers
from starlette.types import Scope

# Same rule as pr-agentic-service; anything else is dropped so a caller
# cannot forge log content.
REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def caller_request_id(scope: Scope) -> str | None:
    value = Headers(scope=scope).get("X-Request-ID")
    if value is None or not REQUEST_ID.fullmatch(value):
        return None
    return value
