"""Tenant identifiers and the per-tenant Redis event Stream they select."""

import re

TENANT_ID_PATTERN = r"^[a-z][a-z0-9-]{1,62}[a-z0-9]$"
_TENANT_ID = re.compile(TENANT_ID_PATTERN)
# Suffixes that name, or may later name, Executor's own Streams.
RESERVED_TENANT_IDS = frozenset({"dlq", "work"})


def is_valid_tenant_id(value: str) -> bool:
    return _TENANT_ID.fullmatch(value) is not None


def tenant_event_stream(event_stream: str, tenant_id: str) -> str:
    """The Redis Stream carrying one tenant's Execution events."""

    return f"{event_stream}.{tenant_id}"
