"""Opt-in real POSIX identity checks in a disposable root-run container."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

WRITER = """
import json, os, sys
from pathlib import Path
from executor_resource_extension.storage import RuntimeStorage
os.umask(0o022)
root = Path(sys.argv[1])
storage = RuntimeStorage(root)
workspace = 'users/u/projects/p/sessions/s/executions/e'
notebook_path = workspace + '/notebooks/execution.ipynb'
if sys.argv[2] == 'prepare':
    storage.prepare_workspace(workspace)
    storage.prepare_notebook(
        workspace_path=workspace, execution_id='e', runtime_profile='basic',
        cells=[{'sequence': 0, 'operation_id': 'o', 'step_id': 's',
                'source': "print('shared notebook')"}])
else:
    notebook = json.loads((root / notebook_path).read_text())
    notebook['cells'][0]['execution_count'] = 1
    notebook['cells'][0]['outputs'] = [
        {'output_type': 'stream', 'name': 'stdout',
         'text': 'shared notebook\\n'}]
    storage.project_notebook(notebook_path=notebook_path, notebook=notebook)
"""

READER = """
import sys
from pathlib import Path
import nbformat
from nbconvert import HTMLExporter
notebook = nbformat.read(Path(sys.argv[1]), as_version=4)
nbformat.validate(notebook)
html, _ = HTMLExporter().from_notebook_node(notebook)
assert 'shared notebook' in html
print('READ_AND_RENDER_OK')
"""


@unittest.skipUnless(
    sys.platform == "linux" and os.geteuid() == 0,
    "Run in a disposable Linux container as root to switch test identities",
)
class SharedNotebookReadTests(unittest.TestCase):
    def test_nobody_can_read_and_render_jupyter_owned_notebooks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o755)
            shared = root / "shared"
            shared.mkdir(mode=0o755)
            os.chown(shared, 1000, 100)
            reader_home = root / "reader-home"
            reader_home.mkdir(mode=0o700)
            os.chown(reader_home, 65534, 65534)
            environment = {
                **os.environ,
                "HOME": str(reader_home),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            path = shared / (
                "users/u/projects/p/sessions/s/executions/e/"
                "notebooks/execution.ipynb"
            )

            def write(action: str) -> None:
                subprocess.run(
                    [sys.executable, "-B", "-c", WRITER, str(shared), action],
                    user=1000,
                    group=100,
                    extra_groups=[],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env=environment,
                )

            def read() -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [sys.executable, "-B", "-c", READER, str(path)],
                    user=65534,
                    group=65534,
                    extra_groups=[],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env=environment,
                )

            write("prepare")
            self.assertEqual(path.stat().st_uid, 1000)
            self.assertEqual(path.stat().st_gid, 100)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
            initial = read()
            self.assertEqual(initial.returncode, 0, initial.stderr)
            self.assertIn("READ_AND_RENDER_OK", initial.stdout)

            # Reproduce the deployment's old file mode, then replace it through
            # the same projection function used by the Jupyter endpoint.
            path.chmod(0o600)
            blocked = read()
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("PermissionError", blocked.stderr)
            for _ in range(2):
                write("project")
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
                projected = read()
                self.assertEqual(projected.returncode, 0, projected.stderr)
                self.assertIn("READ_AND_RENDER_OK", projected.stdout)


if __name__ == "__main__":
    unittest.main()
