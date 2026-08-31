import hashlib
import shutil
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest

from deploy.jupyter.package import (
    EXTENSION_ROOT,
    create_archive,
    delivery_files,
)

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "deploy/jupyter"
HARNESS = REPO / "test_harness/jupyter"


def test_standalone_dockerfile_uses_only_local_copy_sources() -> None:
    dockerfile = (PACKAGE / "Dockerfile").read_text()
    assert "test_harness/" not in dockerfile
    assert dockerfile == (HARNESS / "Dockerfile").read_text().replace(
        "test_harness/jupyter/", ""
    )
    for line in dockerfile.splitlines():
        if not line.startswith("COPY "):
            continue
        arguments = [
            part for part in line.split()[1:] if not part.startswith("--")
        ]
        for source in arguments[:-1]:
            assert not Path(source).is_absolute()
            assert ".." not in Path(source).parts
            assert (PACKAGE / source).exists()


def test_delivery_extension_matches_executor_runtime_contract() -> None:
    source_root = HARNESS / EXTENSION_ROOT
    files = list(source_root.rglob("*.py"))
    assert files
    for source in files:
        relative = source.relative_to(HARNESS)
        assert (PACKAGE / relative).read_bytes() == source.read_bytes(), (
            relative
        )
    for relative in [
        "jupyter_server_config.py",
        "executor_resource_extension.json",
        "start-jupyter.sh",
    ]:
        assert (PACKAGE / relative).read_bytes() == (
            HARNESS / relative
        ).read_bytes()


@pytest.fixture
def copied_package(tmp_path: Path) -> Path:
    target = tmp_path / "standalone"
    shutil.copytree(
        PACKAGE, target, ignore=shutil.ignore_patterns("dist", "__pycache__")
    )
    return target


def test_archive_is_allowlisted_reproducible_and_independent(
    copied_package: Path, tmp_path: Path
) -> None:
    (copied_package / ".env").write_text("JUPYTER_TOKEN=must-not-ship")
    (copied_package / "workspace").mkdir()
    (copied_package / "workspace/private.ipynb").write_text("must-not-ship")
    (copied_package / ".git").mkdir()
    (copied_package / ".git/config").write_text("must-not-ship")
    output, checksum = create_archive(
        copied_package, tmp_path / "delivery.zip"
    )
    assert checksum == hashlib.sha256(output.read_bytes()).hexdigest()
    assert (
        output.with_suffix(".zip.sha256").read_text()
        == f"{checksum}  delivery.zip\n"
    )
    expected = {
        f"executor-jupyter/{path.as_posix()}"
        for path in delivery_files(copied_package)
    }
    with ZipFile(output) as archive:
        assert set(archive.namelist()) == expected
        assert archive.testzip() is None
        assert "executor-jupyter/.env.example" in expected
        assert "executor-jupyter/.env" not in expected
        for name in archive.namelist():
            assert ".." not in Path(name).parts
            assert b"must-not-ship" not in archive.read(name)
        archive.extractall(tmp_path / "extracted")
    standalone = tmp_path / "extracted/executor-jupyter"
    regenerated = tmp_path / "regenerated.zip"
    result = subprocess.run(
        [
            sys.executable,
            str(standalone / "package.py"),
            "--output",
            str(regenerated),
        ],
        cwd=standalone,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert checksum in result.stdout
    assert regenerated.read_bytes() == output.read_bytes()


def test_archive_rejects_missing_required_files(
    copied_package: Path, tmp_path: Path
) -> None:
    (copied_package / "start-jupyter.sh").unlink()
    with pytest.raises(ValueError, match="Missing"):
        create_archive(copied_package, tmp_path / "delivery.zip")


def test_archive_rejects_symlinks(
    copied_package: Path, tmp_path: Path
) -> None:
    target = copied_package / "Dockerfile"
    target.unlink()
    outside = tmp_path / "private"
    outside.write_text("must-not-ship")
    try:
        target.symlink_to(outside)
    except OSError:
        # Some Windows test users cannot create symlinks.
        pytest.skip()
    with pytest.raises(ValueError, match="symlinked"):
        create_archive(copied_package, tmp_path / "delivery.zip")
