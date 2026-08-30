"""Append Agent-authored Markdown to an Execution notebook idempotently."""

import json

import nbformat

from executor_service.domain.errors import ArtifactRegistrationError
from executor_service.domain.runtime import RuntimeStorageAccess
from executor_service.infrastructure.db.models import ExecutionORM


async def append_notebook_markdown(
    runtime_storage: RuntimeStorageAccess,
    execution: ExecutionORM,
    idempotency_key: str,
    content: str,
) -> None:
    if execution.notebook_path is None:
        raise ArtifactRegistrationError("Execution notebook is not available.")
    notebook = await runtime_storage.read_notebook(
        execution.runtime_type,
        execution.runtime_target_id,
        execution.notebook_path,
    )
    document = nbformat.from_dict(notebook)
    if not any(
        cell.get("metadata", {}).get("executor", {}).get("idempotency_key")
        == idempotency_key
        for cell in document.cells
    ):
        document.cells.append(
            nbformat.v4.new_markdown_cell(
                source=content,
                metadata={"executor": {"idempotency_key": idempotency_key}},
            )
        )
        await runtime_storage.write_notebook(
            execution.runtime_type,
            execution.runtime_target_id,
            execution.notebook_path,
            json.loads(nbformat.writes(document)),
        )
