import hashlib
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from executor_service.domain.tenants import (
    RESERVED_TENANT_IDS,
    TENANT_ID_PATTERN,
)
from executor_service.settings import AuthClient

SCRIPT = Path(__file__).parents[1] / "scripts" / "generate_api_keys.py"


def _run(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        input=stdin,
        capture_output=True,
        text=True,
    )


def _generate(*args: str) -> tuple[AuthClient, str]:
    result = _run(*args)
    assert result.returncode == 0, result.stderr
    entry, key_block = result.stdout.split("\n\n")
    (client,) = yaml.safe_load(entry)
    return AuthClient(**client), key_block.splitlines()[-1]


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["ops", "--admin"], {"admin": True}),
        (
            [
                "pr-agentic-service",
                "--tenant",
                "dtest-agent-service",
                "--relay",
                "*",
            ],
            {
                "tenant": "dtest-agent-service",
                "relay": True,
                "allowed_tenants": ("*",),
            },
        ),
        (
            ["pr-batch", "--relay", "a-1,b-2"],
            {"relay": True, "allowed_tenants": ("a-1", "b-2")},
        ),
        (
            ["report-service", "--tenant", "report"],
            {"tenant": "report", "relay": False},
        ),
    ],
)
def test_entry_is_a_valid_auth_client_matching_its_key(args, expected) -> None:
    client, key = _generate("prd", *args)
    assert client.name == args[0]
    assert key.startswith(f"exe_prd_{args[0]}_")
    assert client.key_sha256 == (hashlib.sha256(key.encode()).hexdigest(),)
    for field, value in expected.items():
        assert getattr(client, field) == value


@pytest.mark.parametrize(
    "args",
    [
        ["prd", "x"],  # no kind
        ["prd", "x", "--admin", "--tenant", "report"],
        ["prd", "x", "--admin", "--relay", "*"],
        ["prd", "x", "--tenant", "work"],
        ["prd", "x", "--relay", "*,report"],
        ["prd", "x" * 65, "--admin"],
        ["qa", "x", "--admin"],
    ],
)
def test_entries_executor_would_refuse_are_rejected(args) -> None:
    assert _run(*args).returncode != 0


def test_hash_reads_the_key_from_stdin() -> None:
    result = _run(
        "--hash", stdin="exe_local_ops_0123456789abcdef0123456789abcd\n"
    )
    assert result.stdout.strip() == (
        "364d317335897db4f6e2660edcf5f3efe36edaf5011abed7ee1c32c868651e85"
    )


def test_tenant_rules_match_executor() -> None:
    source = SCRIPT.read_text()
    assert TENANT_ID_PATTERN in source
    assert all(f'"{tenant}"' in source for tenant in RESERVED_TENANT_IDS)
