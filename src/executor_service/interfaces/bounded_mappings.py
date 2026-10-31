"""Size limits for caller-owned mappings stored with an Execution.

Metadata and Step input parameters are stored per row and some are echoed
in events, so each stays small. Request bodies are capped separately.
"""

import json
import re
from typing import Annotated, Any

from pydantic import AfterValidator

MAPPING_MAX_KEYS = 32
MAPPING_MAX_BYTES = 4096
METADATA_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _bounded(value: dict[str, Any]) -> dict[str, Any]:
    if len(value) > MAPPING_MAX_KEYS:
        raise ValueError(f"allows at most {MAPPING_MAX_KEYS} keys.")
    size = len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    )
    if size > MAPPING_MAX_BYTES:
        raise ValueError(
            f"must serialize to at most {MAPPING_MAX_BYTES} bytes."
        )
    return value


def _metadata(value: dict[str, Any]) -> dict[str, Any]:
    invalid = [key for key in value if not METADATA_KEY_PATTERN.match(key)]
    if invalid:
        raise ValueError(
            f"keys must match {METADATA_KEY_PATTERN.pattern}: {invalid}"
        )
    return _bounded(value)


# Caller identifiers: 32 keys matching METADATA_KEY_PATTERN, 4096 bytes.
CallerMetadata = Annotated[dict[str, Any], AfterValidator(_metadata)]
# Tool parameters keep the caller's key names: 32 keys, 4096 bytes.
BoundedParameters = Annotated[dict[str, Any], AfterValidator(_bounded)]
