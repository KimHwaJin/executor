from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from executor_resource_extension.storage import (
    RuntimeStorage,
    StoragePathError,
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
            plot = root / workspace / "artifacts/plots/chart.png"
            plot.write_bytes(b"plot")

            snapshot = storage.snapshot(workspace)
            metadata = storage.file_metadata(plot.as_posix())
            checkpoint_directory_exists = checkpoint_directory.is_dir()
            reports_directory_exists = reports_directory.is_dir()
            legacy_checkpoint_directory_exists = (
                root / workspace / "checkpoints"
            ).exists()

        self.assertEqual(
            prepared["notebook_path"], f"{workspace}/notebooks/execution.ipynb"
        )
        self.assertFalse(checkpoint_directory_exists)
        self.assertTrue(reports_directory_exists)
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


if __name__ == "__main__":
    unittest.main()
