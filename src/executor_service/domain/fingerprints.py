"""Request fingerprints that idempotent commands store and compare."""

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def canonical_sha256(payload: Mapping[str, Any]) -> str:
    """SHA-256 of key-sorted, compact JSON.

    Stored receipts hold these values, so the encoding must never change.
    """
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
