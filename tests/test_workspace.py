import pytest

from executor_service.domain.enums import (
    OperationMode,
    RuntimePool,
    TriggerType,
)
from executor_service.domain.models import Execution, ExecutionStep
from executor_service.infrastructure.workspace import (
    WorkspaceManager,
    WorkspacePathError,
)


def execution(
    *, user_id: str = "user-1", codes: tuple[str, ...] = ("print(1)",)
) -> Execution:
    return Execution(
        idempotency_key="workspace-test",
        request_fingerprint="fingerprint",
        operation_mode=OperationMode.SINGLE,
        trigger_type=TriggerType.INTERACTIVE,
        runtime_pool=RuntimePool.INTERACTIVE,
        runtime_profile="basic",
        user_id=user_id,
        project_id="project-1",
        session_id="session-1",
        task_id="test-task",
        steps=[
            ExecutionStep(
                sequence=sequence,
                code=code,
            )
            for sequence, code in enumerate(codes)
        ],
    )


def test_workspace_only_builds_runtime_paths_and_notebook_document() -> None:
    manager = WorkspaceManager()
    item = execution(codes=("print(1)", "2 + 2"))
    workspace = manager.plan(item)
    cells = manager.load_cells(item)

    assert workspace.runtime_relative_path.startswith(
        "users/user-1/projects/project-1/sessions/session-1/executions/"
    )
    assert workspace.notebook_path.endswith("/notebooks/execution.ipynb")
    assert workspace.reports_path.endswith("/reports")
    assert workspace.artifacts_path.endswith("/artifacts")
    assert cells == ["print(1)", "2 + 2"]

    notebook = manager.notebook_document(
        workspace,
        "python-analysis-a",
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
    assert len(notebook["cells"]) == 2
    assert notebook["metadata"]["kernelspec"]["name"] == "python-analysis-a"
    assert notebook["cells"][1]["outputs"][0]["data"]["text/plain"] == "4"


def test_workspace_rejects_unsafe_external_id() -> None:
    manager = WorkspaceManager()
    with pytest.raises(WorkspacePathError, match="unsafe"):
        manager.plan(execution(user_id="../escape"))
