"""Artifact names are filesystem names, not Executor identifier segments."""

import os
from pathlib import Path

import httpx
import pytest
from executor_resource_extension.storage import (
    RuntimeStorage,
    StoragePathError,
)

from executor_service.domain.errors import ArtifactRegistrationError
from executor_service.domain.runtime import RuntimeDriverError
from executor_service.infrastructure.artifacts.validation import (
    manifest_runtime_path,
)
from executor_service.infrastructure.jupyter.transport import (
    JupyterHttpTransport,
)
from executor_service.infrastructure.workspace import ExecutionWorkspace

NAMES = [
    "histogram_eda__M2 MASK MACRO.max_val__histogram__70a10a10.png",
    "한글 결과 (최종).png",
    "_result.png",
    ".hidden.png",
    "file..png",
    "a%20b #1+.png",
]


@pytest.mark.parametrize("prefix", ["", "users/u/", "/root/"])
@pytest.mark.parametrize("suffix", ["a/../b", "a\\b", "a\nb"])
def test_manifest_rejects_unsafe_paths_in_all_forms(
    prefix: str, suffix: str
) -> None:
    workspace = ExecutionWorkspace("e", "e/n", "e/r", "e/a", "e/m")
    with pytest.raises(ArtifactRegistrationError):
        manifest_runtime_path(prefix + suffix, workspace)


def test_manifest_keeps_original_name() -> None:
    workspace = ExecutionWorkspace("e", "e/n", "e/r", "e/a", "e/m")
    path = "artifacts/plots/한글 (EDA)/M2 MASK.png"
    assert manifest_runtime_path(path, workspace) == f"e/{path}"


@pytest.mark.parametrize("name", NAMES)
def test_snapshot_metadata_download_agree(tmp_path: Path, name: str) -> None:
    storage = RuntimeStorage(tmp_path)
    storage.prepare_workspace("execution")
    relative = f"execution/artifacts/plots/폴더 (EDA)/{name}"
    file = tmp_path / relative
    file.parent.mkdir()
    file.write_bytes(b"original content")
    snapshot = storage.snapshot("execution")
    assert [item["path"] for item in snapshot["files"]] == [relative]
    assert storage.file_metadata(relative)["size_bytes"] == 16
    assert storage.resolve_file(relative).read_bytes() == b"original content"
    assert file.name == name


@pytest.mark.parametrize(
    "path",
    ["a/../b", "../b", "a\\b", "a\x00b", "a\nb", "a\tb", "a\x7fb", "a\x85b"],
)
def test_rejects_forbidden_paths_before_filesystem(
    tmp_path: Path, path: str
) -> None:
    storage = RuntimeStorage(tmp_path)
    for method in (storage.file_metadata, storage.resolve_file):
        with pytest.raises(StoragePathError):
            method(path)
        with pytest.raises(StoragePathError):
            method(f"{tmp_path.as_posix()}/{path}")


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink test")
def test_snapshot_cannot_follow_external_artifact_link(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    storage = RuntimeStorage(root)
    storage.prepare_workspace("execution")
    outside = tmp_path / "private"
    outside.write_bytes(b"private")
    relative = "execution/artifacts/plots/link.png"
    (root / relative).symlink_to(outside)
    for method, argument in (
        (storage.snapshot, "execution"),
        (storage.file_metadata, relative),
        (storage.resolve_file, relative),
    ):
        with pytest.raises(StoragePathError, match="escapes"):
            method(argument)


def test_directory_is_not_a_downloadable_file(tmp_path: Path) -> None:
    storage = RuntimeStorage(tmp_path)
    (tmp_path / "폴더").mkdir()
    for method in (storage.file_metadata, storage.resolve_file):
        with pytest.raises(StoragePathError, match="not a file"):
            method("폴더")


@pytest.mark.skipif(os.name != "posix", reason="POSIX control-character names")
@pytest.mark.parametrize("name", ["bad\nplot.png", "bad\\plot.png"])
def test_snapshot_rejects_forbidden_existing_names(
    tmp_path: Path, name: str
) -> None:
    storage = RuntimeStorage(tmp_path)
    storage.prepare_workspace("execution")
    relative = f"execution/artifacts/plots/{name}"
    (tmp_path / relative).write_bytes(b"plot")
    for method, argument in (
        (storage.snapshot, "execution"),
        (storage.file_metadata, relative),
        (storage.resolve_file, relative),
    ):
        with pytest.raises(StoragePathError):
            method(argument)


@pytest.mark.parametrize(
    ("reason", "visible"),
    [
        ("Path contains a control character.", True),
        ("secret token data", False),
    ],
)
async def test_metadata_error_keeps_only_safe_reason(
    reason: str, visible: bool
) -> None:
    transport = JupyterHttpTransport("http://runtime", "secret", 5, 5)
    await transport.client.aclose()
    transport.client = httpx.AsyncClient(
        base_url="http://runtime",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(422, json={"message": reason})
        ),
    )
    try:
        with pytest.raises(RuntimeDriverError) as error:
            await transport.request(
                "POST", "/executor/storage/files/metadata", json={"path": "a"}
            )
        assert (reason in str(error.value)) is visible
    finally:
        await transport.close()
