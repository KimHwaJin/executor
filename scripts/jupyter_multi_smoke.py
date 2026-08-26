"""Verify append-only MULTI execution on one retained Jupyter kernel."""

import asyncio
import hashlib
import json
from uuid import UUID, uuid4

from executor_service.application.commands import (
    CreateOperationCommand,
    FinalizeExecutionCommand,
    StepSpec,
    SubmitExecutionCommand,
)
from executor_service.config import get_settings
from executor_service.container import ApplicationContainer
from executor_service.domain.enums import (
    ExecutionStatus,
    OperationMode,
    StepStatus,
    TriggerType,
)
from executor_service.domain.models import Execution


async def _wait_for(
    container: ApplicationContainer,
    execution_id: UUID,
    status: ExecutionStatus,
) -> Execution:
    for _ in range(300):
        execution = await container.execution_service.get(execution_id)
        if execution.status == status:
            return execution
        if execution.status.is_terminal and execution.status != status:
            raise RuntimeError(f"Execution ended unexpectedly: {execution}")
        await asyncio.sleep(0.2)
    raise RuntimeError(f"Execution did not reach {status}.")


async def main() -> None:
    unique = str(uuid4())
    settings = get_settings()
    container = ApplicationContainer(settings)
    await container.start()
    try:
        first_code = "value = 40\nprint(value)"
        submitted = await container.execution_service.submit(
            SubmitExecutionCommand(
                idempotency_key=f"multi-submit-{unique}",
                operation_mode=OperationMode.MULTI,
                operation_wait_timeout_seconds=3600,
                trigger_type=TriggerType.INTERACTIVE,
                runtime_profile="basic",
                user_id="multi-user",
                project_id="multi-project",
                session_id="multi-session",
                task_id="test-task",
                steps=(
                    StepSpec(
                        sequence=0,
                        code=first_code,
                        source_sha256=hashlib.sha256(
                            first_code.encode()
                        ).hexdigest(),
                        tool_name="initialize_value",
                    ),
                ),
            )
        )
        first = await _wait_for(
            container, submitted.id, ExecutionStatus.WAITING_FOR_OPERATION
        )
        runtime_session_id = first.runtime_session_id
        if runtime_session_id is None:
            raise RuntimeError("Multi execution did not retain a kernel.")

        second = await container.execution_service.create_operation(
            CreateOperationCommand(
                execution_id=submitted.id,
                idempotency_key=f"multi-continue-1-{unique}",
                expected_version=first.version,
                steps=(
                    StepSpec(
                        sequence=1,
                        code="answer = value + 2\nprint(answer)",
                        source_sha256=hashlib.sha256(
                            b"answer = value + 2\nprint(answer)"
                        ).hexdigest(),
                        tool_name="calculate_answer",
                    ),
                ),
            )
        )
        second = await _wait_for(
            container, second.id, ExecutionStatus.WAITING_FOR_OPERATION
        )
        if second.runtime_session_id != runtime_session_id:
            raise RuntimeError(
                "Multi execution changed kernels between cells."
            )

        failed = await container.execution_service.create_operation(
            CreateOperationCommand(
                execution_id=submitted.id,
                idempotency_key=f"multi-continue-error-{unique}",
                expected_version=second.version,
                steps=(
                    StepSpec(
                        sequence=2,
                        code="raise ValueError('planned multi failure')",
                        source_sha256=hashlib.sha256(
                            b"raise ValueError('planned multi failure')"
                        ).hexdigest(),
                        tool_name="failing_tool",
                    ),
                ),
            )
        )
        failed = await _wait_for(
            container, failed.id, ExecutionStatus.WAITING_FOR_OPERATION
        )
        if failed.steps[-1].status != StepStatus.FAILED:
            raise RuntimeError(
                "Cell error did not return to multi waiting state."
            )

        corrected = await container.execution_service.create_operation(
            CreateOperationCommand(
                execution_id=submitted.id,
                idempotency_key=f"multi-continue-corrected-{unique}",
                expected_version=failed.version,
                steps=(
                    StepSpec(
                        sequence=3,
                        code="corrected = answer * 2\nprint(corrected)",
                        source_sha256=hashlib.sha256(
                            b"corrected = answer * 2\nprint(corrected)"
                        ).hexdigest(),
                        tool_name="corrected_tool",
                    ),
                ),
            )
        )
        corrected = await _wait_for(
            container, corrected.id, ExecutionStatus.WAITING_FOR_OPERATION
        )
        finishing = await container.execution_service.finalize_execution(
            FinalizeExecutionCommand(
                execution_id=submitted.id,
                idempotency_key=f"multi-finish-{unique}",
                expected_version=corrected.version,
            )
        )
        finished = await _wait_for(
            container, finishing.id, ExecutionStatus.SUCCEEDED
        )
        attempts = await container.execution_queries.attempts(submitted.id)
        events = await container.execution_queries.events(submitted.id)
        if finished.notebook_path is None:
            raise RuntimeError(
                "Multi execution did not persist a notebook path."
            )
        notebook_data = await container.runtime_storage.read_notebook(
            finished.runtime_type,
            finished.runtime_target_id,
            finished.notebook_path,
        )
        notebook_text = json.dumps(notebook_data)
        event_types = {event.event_type for event in events}
        cells = notebook_data.get("cells")
        if (
            len(attempts) != 1
            or not isinstance(cells, list)
            or len(cells) != 4
        ):
            raise RuntimeError(
                "Multi Attempt or notebook history is incomplete."
            )
        if not all(
            marker in notebook_text
            for marker in ("42", "84", "planned multi failure")
        ):
            raise RuntimeError(
                "Multi notebook does not prove same-kernel state continuity."
            )
        if not {
            "execution.operation_started",
            "execution.step_started",
            "execution.step_completed",
            "execution.operation_completed",
            "execution.completed",
        }.issubset(event_types):
            raise RuntimeError(
                f"Multi Outbox event history is incomplete: {event_types}"
            )
        if finished.runtime_session_id is not None:
            raise RuntimeError("Finished multi execution retained its kernel.")

        print("execution_id:", submitted.id)
        print("retained_runtime_session:", runtime_session_id)
        print("status:", finished.status.value)
        print("attempts:", len(attempts))
        print("step_statuses:", [step.status.value for step in finished.steps])
        print("notebook_path:", finished.notebook_path)
    finally:
        await container.stop()


if __name__ == "__main__":
    asyncio.run(main())
