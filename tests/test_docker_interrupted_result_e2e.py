"""Safety and evidence-reader tests for the real-process Docker harness."""

import hashlib
import json
import runpy
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from executor_service.interfaces._contracts.execution_inputs import (
    ExecutionSubmitRequest,
)

SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
try:
    HARNESS = runpy.run_path(str(SCRIPTS / "docker_interrupted_result_e2e.py"))
    PROBE = runpy.run_path(str(SCRIPTS / "interrupted_result_probe.py"))
finally:
    sys.path.remove(str(SCRIPTS))


@pytest.mark.parametrize("mode", ["SINGLE", "MULTI"])
@pytest.mark.parametrize("profile", ["basic", "ml"])
def test_case_request_conforms_to_current_contract(
    mode: str, profile: str
) -> None:
    request = ExecutionSubmitRequest.model_validate(
        HARNESS["case_request"](mode, profile, "test-request")
    )
    assert len(request.operation.spec.steps) == 3
    assert request.runtime.profile == profile
    assert request.operation.operation_timeout_seconds == 300


async def test_stop_owner_rejects_unmanaged_service() -> None:
    with pytest.raises(ValueError, match="unmanaged"):
        await HARNESS["stop_owner"](None, "executor")


@pytest.mark.parametrize(
    "service,action",
    [
        ("executor", "kill"),
        ("postgres", "pause"),
        ("executor-primary", "unknown"),
    ],
)
async def test_hard_loss_rejects_unmanaged_targets(
    service: str, action: str
) -> None:
    with pytest.raises(ValueError, match="unmanaged"):
        await HARNESS["interrupt_owner"](None, service, action)


@pytest.mark.parametrize("action", ["kill", "pause"])
async def test_hard_loss_uses_only_owned_compose_service(action: str) -> None:
    calls = []

    class Compose:
        async def kill(self, service: str) -> None:
            calls.append(("kill", service))

        async def run(self, *arguments: str) -> None:
            calls.append(arguments)

    await HARNESS["interrupt_owner"](Compose(), "executor-secondary", action)
    assert calls == [(action, "executor-secondary")]


async def test_cleanup_errors_are_not_silently_ignored() -> None:
    class BrokenCompose(HARNESS["InterruptionCompose"]):
        async def run(self, *arguments: str, **kwargs: object) -> str:
            assert arguments[:2] == ("down", "--volumes")
            assert kwargs.get("tolerate_failure") is not True
            raise RuntimeError("Docker unavailable")

    with pytest.raises(RuntimeError, match="Docker unavailable"):
        await BrokenCompose(None).down()


def write_ref(root: Path, name: str, content: bytes) -> dict[str, Any]:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return {
        "relative_path": name,
        "size_bytes": len(content),
        "checksum_sha256": hashlib.sha256(content).hexdigest(),
    }


def test_checked_file_rejects_corruption_and_escape(tmp_path: Path) -> None:
    ref = write_ref(tmp_path, "output.txt", b"unchanged")
    checked_file = PROBE["checked_file"]
    assert checked_file(tmp_path, ref) == b"unchanged"
    (tmp_path / "output.txt").write_bytes(b"CORRUPTED")
    with pytest.raises(ValueError, match="checksum"):
        checked_file(tmp_path, ref)
    (tmp_path / "output.txt").write_bytes(b"short")
    with pytest.raises(ValueError, match="size"):
        checked_file(tmp_path, ref)
    with pytest.raises(ValueError, match="escaped"):
        checked_file(tmp_path, {**ref, "relative_path": "../outside"})


def test_progress_requires_durably_written_text_and_image(
    tmp_path: Path,
) -> None:
    execution_id = uuid4()
    root = tmp_path / "executions" / str(execution_id) / "1.partial"
    text = write_ref(root, "text.txt", b"before-interrupt:case")
    image = write_ref(root, "plot.png", b"\x89PNG\r\n\x1a\n")
    state = {
        "identity": {"sequence": 1},
        "outputs": [
            {
                "representations": [
                    {**text, "media_type": "text/plain"},
                    {**image, "media_type": "image/png"},
                ]
            }
        ],
    }
    path = root / ".state.json"
    path.write_text(json.dumps(state))
    assert PROBE["progress"](tmp_path, execution_id)
    (root / "plot.png").unlink()
    assert not PROBE["progress"](tmp_path, execution_id)
    path.write_text("{in-progress")
    assert not PROBE["progress"](tmp_path, execution_id)


def test_checkpoint_replaces_report_atomically(tmp_path: Path) -> None:
    path = tmp_path / "reports" / "result.json"
    HARNESS["checkpoint"](path, {"status": "RUNNING"})
    HARNESS["checkpoint"](path, {"status": "PASSED"})
    assert json.loads(path.read_text()) == {"status": "PASSED"}
    assert not path.with_suffix(".json.tmp").exists()
