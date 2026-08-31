"""Create a standalone Jupyter ZIP from an explicit, secret-free file list."""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
import tempfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

FIXED_FILES = (
    ".dockerignore",
    ".env.example",
    "Dockerfile",
    "README.md",
    "SOURCE.md",
    "package.py",
    "jupyter_server_config.py",
    "executor_resource_extension.json",
    "start-jupyter.sh",
    "environments/server/requirements.txt",
    "environments/basic/requirements.txt",
    "environments/ml/requirements.txt",
    "extension/pyproject.toml",
)
EXTENSION_ROOT = "extension/src/executor_resource_extension"


def delivery_files(root: Path) -> list[Path]:
    root = root.resolve(strict=True)
    relatives = [Path(name) for name in FIXED_FILES]
    relatives.extend(
        path.relative_to(root)
        for path in sorted((root / EXTENSION_ROOT).rglob("*.py"))
        if "__pycache__" not in path.parts
    )
    if Path(f"{EXTENSION_ROOT}/__init__.py") not in relatives:
        raise ValueError("Jupyter extension package is missing.")
    for relative in relatives:
        path = root / relative
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Missing or symlinked delivery file: {relative}")
        if not path.resolve(strict=True).is_relative_to(root):
            raise ValueError(f"Delivery file escapes package root: {relative}")
        if any(
            parent.is_symlink() for parent in path.parents if parent != root
        ):
            raise ValueError(f"Symlinked delivery directory: {relative}")
    return sorted(relatives)


def create_archive(root: Path, output: Path) -> tuple[Path, str]:
    root = root.resolve(strict=True)
    output = output.resolve()
    if output.suffix.lower() != ".zip":
        raise ValueError("Output must be a .zip file.")
    relatives = delivery_files(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=output.parent, suffix=".zip", delete=False
        ) as handle:
            temporary = Path(handle.name)
        with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as archive:
            for relative in relatives:
                info = ZipInfo(f"executor-jupyter/{relative.as_posix()}")
                info.create_system = 3
                mode = 0o755 if relative.name == "start-jupyter.sh" else 0o644
                info.external_attr = (stat.S_IFREG | mode) << 16
                info.compress_type = ZIP_DEFLATED
                archive.writestr(info, (root / relative).read_bytes())
        checksum = hashlib.sha256(temporary.read_bytes()).hexdigest()
        os.replace(temporary, output)
        output.with_suffix(".zip.sha256").write_text(
            f"{checksum}  {output.name}\n", encoding="utf-8"
        )
        return output, checksum
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> None:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=root / "dist/executor-jupyter.zip"
    )
    args = parser.parse_args()
    output, checksum = create_archive(root, args.output)
    print(f"Archive: {output}")
    print(f"SHA256: {checksum}")


if __name__ == "__main__":
    main()
