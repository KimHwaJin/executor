"""Tenant identifiers and the per-tenant Redis event Stream they select."""

import re
from collections.abc import Iterable

TENANT_ID_PATTERN = r"^[a-z][a-z0-9-]{1,62}[a-z0-9]$"
_TENANT_ID = re.compile(TENANT_ID_PATTERN)
# Suffixes that name, or may later name, Executor's own Streams.
ALWAYS_RESERVED_TENANT_IDS = frozenset({"dlq", "work"})


def reserved_tenant_ids(
    event_stream: str, stream_names: Iterable[str]
) -> frozenset[str]:
    """Reserved ids, including any internal Stream below the event Stream."""

    prefix = f"{event_stream}."
    return ALWAYS_RESERVED_TENANT_IDS | {
        name.removeprefix(prefix)
        for name in stream_names
        if name.startswith(prefix)
    }


def is_valid_tenant_id(value: str) -> bool:
    return _TENANT_ID.fullmatch(value) is not None


def tenant_event_stream(event_stream: str, tenant_id: str | None) -> str:
    """The Redis Stream carrying one tenant's Execution events."""

    return event_stream if tenant_id is None else f"{event_stream}.{tenant_id}"
