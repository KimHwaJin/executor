import hashlib
import re
import subprocess
import sys
from pathlib import Path

from executor_service.settings import AuthClient

SCRIPT = Path(__file__).parents[1] / "scripts" / "generate_api_keys.py"


def _generate(*args: str) -> list[tuple[str, str, str]]:
    output = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return re.findall(
        r"^(\S+)\n  .*key_sha256: (\S+)\n  .*: (\S+)$", output, re.MULTILINE
    )


def test_every_caller_gets_a_key_matching_a_valid_hash() -> None:
    generated = _generate("prd")

    assert [name for name, _, _ in generated] == [
        "pr-agentic-service",
        "pr-batch",
        "ops",
    ]
    assert len({key for _, _, key in generated}) == 3
    for name, digest, key in generated:
        assert key.startswith("exe_prd_")
        assert hashlib.sha256(key.encode()).hexdigest() == digest
        AuthClient(name=name, key_sha256=(digest,))


def test_caller_option_limits_the_output_for_a_rotation() -> None:
    assert [name for name, _, _ in _generate("stg", "--caller", "ops")] == [
        "ops"
    ]
