import hashlib
import subprocess
import sys
from pathlib import Path

import yaml

from executor_service.settings import AuthClient

SCRIPT = Path(__file__).parents[1] / "scripts" / "generate_api_keys.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
    )


def _generate(*args: str) -> tuple[AuthClient, str]:
    result = _run(*args)
    assert result.returncode == 0, result.stderr
    entry, key = result.stdout.rsplit("\n#", 1)
    return (
        AuthClient(**yaml.safe_load(entry)[0]),
        key.splitlines()[1],
    )


def test_master_key_is_admin_only() -> None:
    client, key = _generate("prd", "--kind", "master", "--caller", "ops")

    assert client.name == "ops"
    assert client.admin
    assert not client.relay
    assert client.tenant is None
    assert key.startswith("exe_prd_master_")
    assert client.key_sha256 == (hashlib.sha256(key.encode()).hexdigest(),)


def test_normal_key_relays_any_tenant_and_is_not_admin() -> None:
    client, key = _generate(
        "stg",
        "--kind",
        "normal",
        "--caller",
        "pr-agentic-service",
        "--tenant",
        "dtest-agent-service",
    )

    assert not client.admin
    assert client.tenant == "dtest-agent-service"
    assert client.allows_tenant("any-new-tenant")
    assert key.startswith("exe_stg_normal_")


def test_invalid_tenant_is_refused() -> None:
    result = _run(
        "prd", "--kind", "normal", "--caller", "x", "--tenant", "dlq"
    )

    assert result.returncode == 2
    assert "exe_" not in result.stdout


def test_master_key_refuses_a_tenant() -> None:
    result = _run(
        "prd", "--kind", "master", "--caller", "ops", "--tenant", "acme"
    )

    assert result.returncode == 2
    assert "exe_" not in result.stdout
