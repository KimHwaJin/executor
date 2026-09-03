from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from executor_resource_extension.storage import (
    RuntimeStorage,
    StoragePathError,
    _atomic_json_write,
)


class RuntimeStorageTests(unittest.TestCase):
    def test_prepares_user_notebook_without_runtime_output_metadata(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = RuntimeStorage(root)
            workspace = "users/u1/projects/p1/sessions/s1/executions/e1"
            storage.prepare_workspace(workspace)

            result = storage.prepare_notebook(
                workspace_path=workspace,
                execution_id="e1",
                runtime_profile="basic",
                cells=[
                    {
                        "sequence": 0,
                        "operation_id": "o1",
                        "step_id": "s1",
                        "source": "print('hello')",
                    }
                ],
            )
            notebook_path = root / result["notebook_path"]
            notebook = json.loads(notebook_path.read_text(encoding="utf-8"))
            if os.name == "posix":
                self.assertEqual(
                    stat.S_IMODE(notebook_path.stat().st_mode), 0o644
                )
                # Reproduce already-generated private notebooks on the PV.
                notebook_path.chmod(0o600)

            self.assertEqual(notebook["cells"][0]["source"], "print('hello')")
            self.assertEqual(notebook["cells"][0]["outputs"], [])
            self.assertFalse((root / workspace / "outputs").exists())
            projected = {
                **notebook,
                "cells": [
                    {
                        **notebook["cells"][0],
                        "outputs": [
                            {
                                "output_type": "stream",
                                "name": "stdout",
                                "text": "hello\n",
                            }
                        ],
                    }
                ],
            }
            projection = storage.project_notebook(
                notebook_path=result["notebook_path"], notebook=projected
            )
            persisted = json.loads(notebook_path.read_text(encoding="utf-8"))
            self.assertEqual(projection["cell_count"], 1)
            self.assertEqual(
                persisted["cells"][0]["outputs"][0]["text"], "hello\n"
            )
            if os.name == "posix":
                self.assertEqual(
                    stat.S_IMODE(notebook_path.stat().st_mode), 0o644
                )
            self.assertFalse(
                (root / workspace / "notebooks/.ipynb_checkpoints").exists()
            )

    def test_prepares_snapshots_and_hashes_runtime_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = RuntimeStorage(root)
            workspace = "users/u1/projects/p1/sessions/s1/executions/e1"
            prepared = storage.prepare_workspace(workspace)
            checkpoint_directory = (
                root / workspace / "notebooks/.ipynb_checkpoints"
            )
            reports_directory = root / workspace / "reports"
            datasets_directory = root / workspace / "artifacts/datasets"
            models_directory = root / workspace / "artifacts/models"
            plot = root / workspace / "artifacts/plots/chart.png"
            plot.write_bytes(b"plot")

            snapshot = storage.snapshot(workspace)
            metadata = storage.file_metadata(plot.as_posix())
            checkpoint_directory_exists = checkpoint_directory.is_dir()
            reports_directory_exists = reports_directory.is_dir()
            datasets_directory_exists = datasets_directory.is_dir()
            models_directory_exists = models_directory.is_dir()
            legacy_checkpoint_directory_exists = (
                root / workspace / "checkpoints"
            ).exists()

        self.assertEqual(
            prepared["notebook_path"], f"{workspace}/notebooks/execution.ipynb"
        )
        self.assertFalse(checkpoint_directory_exists)
        self.assertTrue(reports_directory_exists)
        self.assertFalse(datasets_directory_exists)
        self.assertTrue(models_directory_exists)
        self.assertFalse(legacy_checkpoint_directory_exists)
        self.assertEqual(
            snapshot["files"][0]["path"],
            f"{workspace}/artifacts/plots/chart.png",
        )
        self.assertEqual(metadata["size_bytes"], 4)
        self.assertEqual(
            metadata["checksum_sha256"],
            "0f3850ab36e9d43a8615d62d179e484003562531b41a9f517c5f4e7313b00222",
        )

    def test_reads_only_appended_manifest_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = RuntimeStorage(root)
            workspace = "users/u1/projects/p1/sessions/s1/executions/e1"
            storage.prepare_workspace(workspace)
            manifest = root / workspace / "artifacts/manifest.jsonl"
            manifest.write_text("one\ntwo\n", encoding="utf-8")

            result = storage.read_manifest(workspace, 4)

        self.assertEqual(result, {"start": 4, "end": 8, "content": "two\n"})

    def test_rejects_paths_outside_jupyter_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = RuntimeStorage(root)
            with self.assertRaises(StoragePathError):
                storage.prepare_workspace("../escape")
            with self.assertRaises(StoragePathError):
                storage.file_metadata("/etc/passwd")
            with self.assertRaises(StoragePathError):
                storage.resolve_file("../escape")

    def test_resolves_download_file_only_below_jupyter_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = RuntimeStorage(root)
            file_path = root / "users/u1/artifacts/plots/chart.png"
            file_path.parent.mkdir(parents=True)
            file_path.write_bytes(b"png")

            resolved = storage.resolve_file(
                "users/u1/artifacts/plots/chart.png"
            )

            self.assertEqual(resolved, file_path.resolve())


@unittest.skipUnless(
    os.name == "posix", "POSIX file modes do not model NTFS ACLs"
)
class NotebookPermissionTests(unittest.TestCase):
    def test_sets_read_permissions_before_replace_regardless_of_umask(
        self,
    ) -> None:
        document = {"cells": [], "metadata": {"title": "노트북"}}
        replace = os.replace
        for original_mode in (None, 0o600, 0o644, 0o744):
            with (
                self.subTest(original_mode=original_mode),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                path = root / "execution.ipynb"
                if original_mode is not None:
                    path.write_bytes(b"old notebook")
                    path.chmod(original_mode)
                unrelated = root / "private-data.csv"
                unrelated.write_bytes(b"private")
                unrelated.chmod(0o600)
                original_directory_mode = stat.S_IMODE(root.stat().st_mode)

                def checked_replace(
                    source: Path,
                    target: Path,
                    mode: int | None = original_mode,
                ) -> None:
                    self.assertEqual(
                        stat.S_IMODE(source.stat().st_mode), 0o644
                    )
                    self.assertEqual(json.loads(source.read_bytes()), document)
                    if mode is None:
                        self.assertFalse(target.exists())
                    else:
                        self.assertEqual(target.read_bytes(), b"old notebook")
                    replace(source, target)

                old_umask = os.umask(0o077)
                try:
                    with patch(
                        "executor_resource_extension.storage.os.replace",
                        side_effect=checked_replace,
                    ) as replaced:
                        _atomic_json_write(path, document)
                    replaced.assert_called_once()
                finally:
                    os.umask(old_umask)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
                self.assertEqual(json.loads(path.read_bytes()), document)
                self.assertEqual(stat.S_IMODE(unrelated.stat().st_mode), 0o600)
                self.assertEqual(
                    stat.S_IMODE(root.stat().st_mode), original_directory_mode
                )
                self.assertEqual(list(root.glob(".execution.ipynb.*")), [])

    def test_failed_write_does_not_publish_or_leave_temporary_files(
        self,
    ) -> None:
        for operation in ("fchmod", "fsync", "replace"):
            for existing in (False, True):
                with (
                    self.subTest(operation=operation, existing=existing),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    root = Path(directory)
                    path = root / "execution.ipynb"
                    if existing:
                        path.write_bytes(b"old notebook")
                        path.chmod(0o600)
                    with (
                        patch(
                            f"executor_resource_extension.storage.os.{operation}",
                            side_effect=PermissionError(
                                "injected write failure"
                            ),
                        ),
                        self.assertRaises(PermissionError),
                    ):
                        _atomic_json_write(path, {"cells": []})
                    if existing:
                        self.assertEqual(path.read_bytes(), b"old notebook")
                        self.assertEqual(
                            stat.S_IMODE(path.stat().st_mode), 0o600
                        )
                    else:
                        self.assertFalse(path.exists())
                    self.assertEqual(list(root.glob(".execution.ipynb.*")), [])


if __name__ == "__main__":
    unittest.main()
