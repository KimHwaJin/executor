import errno
import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from executor_service.domain.results import StepResultIdentity
from executor_service.domain.runtime import (
    RuntimeOutputRecord,
    RuntimeOutputRepresentation,
)
from executor_service.infrastructure._result_storage.io import atomic_write
from executor_service.infrastructure.result_storage import (
    FilesystemExecutionResultStore,
    ResultStorageError,
)


@pytest.mark.parametrize("terminal", ["FINALIZED", "FAILED", "ABORTED"])
async def test_output_paths_are_pv_relative_across_mounts(
    tmp_path: Path,
    terminal: str,
) -> None:
    root = tmp_path / "executor-pv"
    store = FilesystemExecutionResultStore(root)
    identity = _identity()
    source = await store.snapshot_source(
        identity.execution_id, identity.step_id, "print(1)"
    )
    await store.begin_step_result(identity, source)
    await store.append_step_outputs(
        identity,
        expected_offset=0,
        batch_id=uuid4(),
        records=(_text_record("portable\n"),),
    )
    if terminal == "ABORTED":
        result = await store.abort_step_result(
            identity, reason="test cancellation"
        )
    else:
        result = await store.finalize_step_result(
            identity,
            execution_count=1,
            error_message="test failure" if terminal == "FAILED" else None,
        )
    manifest = json.loads((root / result.reference.relative_path).read_bytes())
    relative = manifest["outputs"][0]["representations"][0]["relative_path"]
    assert (
        relative
        == (
            Path(result.reference.relative_path).parent
            / "outputs/000000-stream-00.txt"
        ).as_posix()
    )
    assert ".partial" not in relative
    assert (root / relative).read_text() == "portable\n"
    # Same shared tree mounted at a different absolute location.
    other_mount = tmp_path / "agent-pv"
    shutil.copytree(root, other_mount)
    reader = FilesystemExecutionResultStore(other_mount)
    assert (await reader.read_step_outputs(result.reference))[0][
        "text"
    ] == "portable\n"
    assert await reader.read_source(source) == "print(1)"


@pytest.mark.parametrize(
    "bad_path", ["absolute", "parent", "symlink", "other-step"]
)
async def test_root_relative_outputs_keep_scope_boundaries(
    tmp_path: Path,
    bad_path: str,
) -> None:
    root = tmp_path / "pv"
    store = FilesystemExecutionResultStore(root)
    identity = _identity()
    source = await store.snapshot_source(
        identity.execution_id, identity.step_id, "pass"
    )
    await store.begin_step_result(identity, source)
    await store.append_step_outputs(
        identity,
        expected_offset=0,
        batch_id=uuid4(),
        records=(_text_record("secret"),),
    )
    result = await store.finalize_step_result(identity, execution_count=1)
    manifest_path = root / result.reference.relative_path
    manifest = json.loads(manifest_path.read_bytes())
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    other = root / "other-step.txt"
    other.write_text("secret")
    link = manifest_path.parent / "outputs/link.txt"
    link.symlink_to(outside)
    path = {
        "absolute": str(outside),
        "parent": "../outside.txt",
        "symlink": link.relative_to(root).as_posix(),
        "other-step": "other-step.txt",
    }[bad_path]
    manifest["outputs"][0]["representations"][0]["relative_path"] = path
    body = json.dumps(manifest).encode()
    manifest_path.write_bytes(body)
    reference = replace(
        result.reference,
        checksum_sha256=hashlib.sha256(body).hexdigest(),
        size_bytes=len(body),
    )
    with pytest.raises(ResultStorageError):
        await store.read_step_outputs(reference)


def _identity(*, fence: int = 1) -> StepResultIdentity:
    return StepResultIdentity(
        execution_id=UUID("10000000-0000-0000-0000-000000000001"),
        operation_id=UUID("20000000-0000-0000-0000-000000000002"),
        step_id=UUID("30000000-0000-0000-0000-000000000003"),
        sequence=0,
        execution_attempt_id=UUID("40000000-0000-0000-0000-000000000004"),
        fencing_token=fence,
    )


def _text_record(content: str = "hello\n") -> RuntimeOutputRecord:
    return RuntimeOutputRecord(
        kind="STREAM",
        stream_name="stdout",
        representations=(
            RuntimeOutputRepresentation(
                media_type="text/plain",
                encoding="UTF8",
                content=content,
            ),
        ),
    )


@pytest.mark.parametrize(
    ("failure_point", "error_number"),
    [("fsync", errno.ENOSPC), ("replace", errno.EACCES)],
)
def test_atomic_write_does_not_publish_partial_file_on_os_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
    error_number: int,
) -> None:
    target = tmp_path / "manifest.json"

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError(error_number, "injected storage failure")

    monkeypatch.setattr(
        "executor_service.infrastructure._result_storage.io.os."
        f"{failure_point}",
        fail,
    )

    with pytest.raises(OSError) as raised:
        atomic_write(target, b'{"complete":true}')

    assert raised.value.errno == error_number
    assert not target.exists()
    assert list(tmp_path.glob(".*.tmp")) == []


@pytest.mark.asyncio
async def test_result_disk_layout_is_a_stable_contract(tmp_path: Path) -> None:
    store = FilesystemExecutionResultStore(tmp_path)
    identity = _identity()
    source = await store.snapshot_source(
        identity.execution_id,
        identity.step_id,
        "print('contract')",
    )
    assert source.relative_path == (
        "executions/10000000-0000-0000-0000-000000000001/sources/"
        "30000000-0000-0000-0000-000000000003/source.py"
    )

    await store.begin_step_result(identity, source)
    partial = (
        tmp_path
        / "executions/10000000-0000-0000-0000-000000000001/operations/"
        "20000000-0000-0000-0000-000000000002/steps/"
        "30000000-0000-0000-0000-000000000003/attempts/"
        "40000000-0000-0000-0000-000000000004/1.partial"
    )
    state = json.loads((partial / ".state.json").read_bytes())
    assert state["schema_version"] == "1.0"
    assert state["state"] == "OPEN"
    assert state["identity"] == {
        "execution_attempt_id": "40000000-0000-0000-0000-000000000004",
        "execution_id": "10000000-0000-0000-0000-000000000001",
        "fencing_token": 1,
        "operation_id": "20000000-0000-0000-0000-000000000002",
        "sequence": 0,
        "step_id": "30000000-0000-0000-0000-000000000003",
    }

    await store.append_step_outputs(
        identity,
        expected_offset=0,
        batch_id=UUID("50000000-0000-0000-0000-000000000005"),
        records=(_text_record("contract\n"),),
    )
    assert (partial / "outputs/000000-stream-00.txt").read_text() == (
        "contract\n"
    )

    result = await store.finalize_step_result(identity, execution_count=7)
    assert result.reference.relative_path == (
        "executions/10000000-0000-0000-0000-000000000001/operations/"
        "20000000-0000-0000-0000-000000000002/steps/"
        "30000000-0000-0000-0000-000000000003/attempts/"
        "40000000-0000-0000-0000-000000000004/1/manifest.json"
    )
    manifest_path = tmp_path / result.reference.relative_path
    assert not partial.exists()
    assert manifest_path.is_file()
    assert (manifest_path.parent / "source.py").read_text() == (
        "print('contract')"
    )
    assert not (manifest_path.parent / ".state.json").exists()


@pytest.mark.asyncio
async def test_seals_source_text_and_image_as_immutable_files(
    tmp_path: Path,
) -> None:
    store = FilesystemExecutionResultStore(tmp_path)
    identity = _identity()
    source = await store.snapshot_source(
        identity.execution_id,
        identity.step_id,
        "print('hello')",
    )
    await store.begin_step_result(identity, source)
    batch = uuid4()
    appended = await store.append_step_outputs(
        identity,
        expected_offset=0,
        batch_id=batch,
        records=(
            _text_record(),
            RuntimeOutputRecord(
                kind="DISPLAY",
                representations=(
                    RuntimeOutputRepresentation(
                        media_type="image/png",
                        encoding="BASE64",
                        content="iVBORw0KGgo=",
                    ),
                ),
            ),
        ),
    )
    assert appended.committed_offset == 2
    result = await store.finalize_step_result(identity, execution_count=1)

    manifest_path = tmp_path / result.reference.relative_path
    manifest_body = manifest_path.read_bytes()
    assert hashlib.sha256(manifest_body).hexdigest() == (
        result.reference.checksum_sha256
    )
    manifest = json.loads(manifest_body)
    assert manifest["state"] == "FINALIZED"
    assert manifest["complete"] is True
    assert result.complete is True
    assert manifest["output_summary"] == {
        "has_error": False,
        "has_image": True,
        "image_count": 1,
        "mime_types": ["image/png", "text/plain"],
        "output_count": 2,
        "output_types": {"display_data": 1, "stream": 1},
        "stream_names": ["stdout"],
    }
    assert (manifest_path.parent / "source.py").read_text() == (
        "print('hello')"
    )
    for output in manifest["outputs"]:
        for representation in output["representations"]:
            assert representation["complete"] is True
            assert representation["truncated_in_preview"] is False
            assert (tmp_path / representation["relative_path"]).is_file()
    assert not manifest_path.parent.with_name("1.partial").exists()
    notebook_outputs = await store.read_step_outputs(result.reference)
    assert notebook_outputs[0] == {
        "output_type": "stream",
        "name": "stdout",
        "text": "hello\n",
    }
    assert notebook_outputs[1]["output_type"] == "display_data"
    assert notebook_outputs[1]["data"] == {"image/png": "iVBORw0KGgo="}
    projection = await store.read_step_projection(result.reference)
    assert projection.outputs == notebook_outputs
    assert projection.execution_count == 1


@pytest.mark.asyncio
async def test_aborted_result_seals_partial_outputs_as_incomplete(
    tmp_path: Path,
) -> None:
    store = FilesystemExecutionResultStore(tmp_path)
    identity = _identity()
    source = await store.snapshot_source(
        identity.execution_id,
        identity.step_id,
        "print('partial')",
    )
    await store.begin_step_result(identity, source)
    await store.append_step_outputs(
        identity,
        expected_offset=0,
        batch_id=uuid4(),
        records=(_text_record("before limit\n"),),
    )

    result = await store.abort_step_result(
        identity,
        reason="Runtime output message exceeded safety limit.",
    )

    manifest = json.loads(
        (tmp_path / result.reference.relative_path).read_bytes()
    )
    assert result.state == "ABORTED"
    assert result.complete is False
    assert manifest["complete"] is False
    assert manifest["total_size_bytes"] == len(b"before limit\n")
    assert "safety limit" in manifest["error_message"]


@pytest.mark.asyncio
async def test_append_batch_replay_is_idempotent(tmp_path: Path) -> None:
    store = FilesystemExecutionResultStore(tmp_path)
    identity = _identity()
    source = await store.snapshot_source(
        identity.execution_id, identity.step_id, "print(1)"
    )
    await store.begin_step_result(identity, source)
    batch = uuid4()
    first = await store.append_step_outputs(
        identity,
        expected_offset=0,
        batch_id=batch,
        records=(_text_record(),),
    )
    replay = await store.append_step_outputs(
        identity,
        expected_offset=0,
        batch_id=batch,
        records=(_text_record(),),
    )
    assert first.replayed is False
    assert replay.replayed is True
    assert replay.committed_offset == 1


@pytest.mark.asyncio
async def test_fencing_generations_use_different_directories(
    tmp_path: Path,
) -> None:
    store = FilesystemExecutionResultStore(tmp_path)
    references = []
    for fence in (1, 2):
        identity = _identity(fence=fence)
        source = await store.snapshot_source(
            identity.execution_id, identity.step_id, "print(1)"
        )
        await store.begin_step_result(identity, source)
        await store.append_step_outputs(
            identity,
            expected_offset=0,
            batch_id=uuid4(),
            records=(_text_record(str(fence)),),
        )
        result = await store.finalize_step_result(
            identity, execution_count=fence
        )
        references.append(result.reference.relative_path)
    assert references[0] != references[1]
    assert all((tmp_path / value).is_file() for value in references)


@pytest.mark.asyncio
async def test_source_reference_cannot_escape_shared_root(
    tmp_path: Path,
) -> None:
    store = FilesystemExecutionResultStore(tmp_path)
    source = await store.snapshot_source(uuid4(), uuid4(), "print(1)")
    invalid = type(source)(
        relative_path="../outside.py",
        checksum_sha256=source.checksum_sha256,
        size_bytes=source.size_bytes,
    )
    with pytest.raises(ResultStorageError, match="unsafe segment"):
        await store.read_source(invalid)
