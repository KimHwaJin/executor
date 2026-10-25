"""Stored receipts compare against these values, so the encoding is fixed."""

import hashlib

from executor_service.domain.fingerprints import canonical_sha256


def test_encoding_matches_stored_receipts() -> None:
    payload = {"desired_state": "DRAINING", "actor_id": "ops-1", "n": 2}
    expected = hashlib.sha256(
        b'{"actor_id":"ops-1","desired_state":"DRAINING","n":2}'
    ).hexdigest()
    assert canonical_sha256(payload) == expected


def test_non_json_values_use_their_string_form() -> None:
    assert canonical_sha256({"at": 1.5, "id": object}) == canonical_sha256(
        {"at": 1.5, "id": str(object)}
    )
