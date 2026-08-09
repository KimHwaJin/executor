from pathlib import Path

import nbformat
import pytest

from executor_service.domain.enums import (
    CodeSourceType,
    ExecutionMode,
    JupyterPool,
    TriggerType,
)
from executor_service.domain.models import Execution
from executor_service.infrastructure.workspace import WorkspaceManager, WorkspacePathError


def execution(*, user_id: str = "user-1", code: str = "print(1)") -> Execution:
    return Execution(
        idempotency_key="workspace-test",
        request_fingerprint="fingerprint",
        mode=ExecutionMode.STATIC,
        trigger_type=TriggerType.INTERACTIVE,
        jupyter_pool=JupyterPool.INTERACTIVE,
        kernel_name="python3",
        code_source_type=CodeSourceType.INLINE,
        code=code,
        code_path=None,
        requested_by_user_id=user_id,
        project_id="project-1",
        session_id="session-1",
        execution_plan_id="plan-1",
    )


def test_workspace_uses_expected_pv_hierarchy_and_writes_notebook(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path)
    item = execution(code="# %%\nprint(1)\n# %%\n2 + 2")
    workspace = manager.prepare(item)
    cells = manager.load_cells(item, workspace)

    assert workspace.host_root.relative_to(tmp_path).parts[:6] == (
        "users",
        "user-1",
        "projects",
        "project-1",
        "sessions",
        "session-1",
    )
    assert cells == ["print(1)", "2 + 2"]

    manager.write_notebook(
        workspace,
        cells,
        [
            [{"output_type": "stream", "name": "stdout", "text": "1\n"}],
            [
                {
                    "output_type": "execute_result",
                    "data": {"text/plain": "4"},
                    "metadata": {},
                    "execution_count": 2,
                }
            ],
        ],
        [1, 2],
    )
    notebook = nbformat.read(workspace.notebook_file, as_version=4)
    assert len(notebook.cells) == 2
    assert notebook.cells[1].outputs[0].data["text/plain"] == "4"


def test_workspace_rejects_unsafe_external_id(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path)
    with pytest.raises(WorkspacePathError, match="unsafe"):
        manager.prepare(execution(user_id="../escape"))
