import runpy
from pathlib import Path

import pytest

from executor_service.settings import AuthClient

SCRIPT = runpy.run_path(
    str(Path(__file__).parents[1] / "scripts" / "generate_api_keys.py"),
    run_name="generate_api_keys_test",
)


@pytest.mark.parametrize("environment", ["dev", "stg", "prd"])
def test_every_configured_caller_gets_a_key_its_hash_accepts(environment):
    clients = SCRIPT["configured_clients"](environment)
    assert clients == ["pr-agentic-service", "pr-batch", "ops"]
    keys = [SCRIPT["new_key"](environment, client) for client in clients]
    assert len(set(keys)) == len(keys)
    for client, key in zip(clients, keys, strict=True):
        assert key.startswith(f"exe_{environment}_{client}_")
        # The printed hash is what AUTH_CLIENTS validates and matches.
        AuthClient(name=client, key_sha256=(SCRIPT["key_sha256"](key),))


def test_hash_matches_the_local_compose_key():
    assert (
        SCRIPT["key_sha256"]("exe_local_ops_0123456789abcdef0123456789abcd")
        == "364d317335897db4f6e2660edcf5f3efe36edaf5011abed7ee1c32c868651e85"
    )
