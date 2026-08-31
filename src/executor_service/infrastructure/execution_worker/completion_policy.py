"""Fenced DB guards for success, after required file work has completed.

No remote I/O is performed while holding the Execution row lock. File integrity
is checked by the result store/projector before entering these transitions.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from executor_service.domain.enums import (
    ArtifactStatus,
    ArtifactType,
    OperationStatus,
    StepStatus,
)
from executor_service.domain.runtime import ExecutionCompletionError
from executor_service.infrastructure.db.models import (
    ExecutionArtifactORM,
    ExecutionOperationORM,
    ExecutionORM,
    ExecutionStepORM,
)


async def require_completed_results(
    session: AsyncSession,
    execution: ExecutionORM,
    operation_id: UUID | None,
    *,
    finalizing: bool = False,
    require_notebook_artifact: bool = True,
) -> None:
    operation = (
        await session.get(ExecutionOperationORM, operation_id)
        if operation_id is not None
        else None
    )
    if (
        operation is None
        or operation.execution_id != execution.id
        or operation.status
        not in {
            OperationStatus.RUNNING,
            OperationStatus.SUCCEEDED,
        }
        or (finalizing and operation.status != OperationStatus.SUCCEEDED)
    ):
        raise ExecutionCompletionError("OPERATION_COMPLETION_CHECK")
    steps = list(
        await session.scalars(
            select(ExecutionStepORM)
            .where(ExecutionStepORM.operation_id == operation_id)
            .order_by(ExecutionStepORM.sequence)
        )
    )
    if not steps or any(
        step.status != StepStatus.SUCCEEDED
        or not step.result_complete
        or not step.result_manifest_path
        or not step.result_manifest_checksum_sha256
        or not step.result_manifest_size_bytes
        or step.result_execution_attempt_id is None
        or step.result_fencing_token is None
        for step in steps
    ):
        raise ExecutionCompletionError("RESULT_COMPLETION_CHECK")
    if execution.notebook_projection_status != "SUCCEEDED":
        raise ExecutionCompletionError("NOTEBOOK_COMPLETION_CHECK")
    if not require_notebook_artifact:
        return
    notebook_id = await session.scalar(
        select(ExecutionArtifactORM.id)
        .where(
            ExecutionArtifactORM.execution_id == execution.id,
            ExecutionArtifactORM.execution_step_id == steps[-1].id,
            ExecutionArtifactORM.artifact_type == ArtifactType.NOTEBOOK,
            ExecutionArtifactORM.status == ArtifactStatus.AVAILABLE,
            ExecutionArtifactORM.relative_path == execution.notebook_path,
        )
        .limit(1)
    )
    if notebook_id is None:
        raise ExecutionCompletionError("NOTEBOOK_ARTIFACT_CHECK")
