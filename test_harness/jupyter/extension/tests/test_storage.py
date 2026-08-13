from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from executor_resource_extension.storage import RuntimeStorage, StoragePathError


class RuntimeStorageTests(unittest.TestCase):
    def test_prepares_snapshots_and_hashes_runtime_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = RuntimeStorage(root)
            workspace = "users/u1/projects/p1/sessions/s1/executions/e1"
            prepared = storage.prepare_workspace(workspace)
            plot = root / workspace / "artifacts/plots/chart.png"
            plot.write_bytes(b"plot")

            snapshot = storage.snapshot(workspace)
            metadata = storage.file_metadata(plot.as_posix())

        self.assertEqual(prepared["notebook_path"], f"{workspace}/notebooks/execution.ipynb")
        self.assertEqual(snapshot["files"][0]["path"], f"{workspace}/artifacts/plots/chart.png")
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


if __name__ == "__main__":
    unittest.main()
