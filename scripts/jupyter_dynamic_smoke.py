"""Verify append-only DYNAMIC execution on one retained Jupyter kernel."""

import asyncio
import json
from pathlib import Path
from uuid import UUID, uuid4

from executor_service.application.commands import (
    ContinueExecutionCommand,
    FinishExecutionCommand,
    StepSpec,
    SubmitExecutionCommand,
)
from executor_service.config import get_settings
from executor_service.container import ApplicationContainer
from executor_service.domain.enums import (
    CodeSourceType,
    ExecutionMode,
    ExecutionStatus,
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
                idempotency_key=f"dynamic-submit-{unique}",
                mode=ExecutionMode.DYNAMIC,
                trigger_type=TriggerType.INTERACTIVE,
                runtime_profile="python3",
                code_source_type=CodeSourceType.INLINE,
                source_content=first_code,
                code_path=None,
                source_sha256="0" * 64,
                user_id="dynamic-user",
                project_id="dynamic-project",
                session_id="dynamic-session",
                task_id="test-task",
                execution_plan_id=f"dynamic-plan-{unique}",
                steps=(
                    StepSpec(
                        sequence=0,
                        code=first_code,
                        execution_plan_id=f"dynamic-plan-{unique}",
                        plan_step_id=f"dynamic-plan-{unique}-step-0",
                        tool_name="initialize_value",
                    ),
                ),
            )
        )
        first = await _wait_for(container, submitted.id, ExecutionStatus.WAITING_FOR_NEXT_STEP)
        runtime_session_id = first.runtime_session_id
        if runtime_session_id is None:
            raise RuntimeError("Dynamic execution did not retain a kernel.")

        second = await container.execution_service.continue_execution(
            ContinueExecutionCommand(
                execution_id=submitted.id,
                idempotency_key=f"dynamic-continue-1-{unique}",
                expected_version=first.version,
                step=StepSpec(
                    sequence=1,
                    code="answer = value + 2\nprint(answer)",
                    execution_plan_id="revision-2",
                    plan_step_id="revision-2-step-1",
                    tool_name="calculate_answer",
                ),
            )
        )
        second = await _wait_for(container, second.id, ExecutionStatus.WAITING_FOR_NEXT_STEP)
        if second.runtime_session_id != runtime_session_id:
            raise RuntimeError("Dynamic execution changed kernels between cells.")

        failed = await container.execution_service.continue_execution(
            ContinueExecutionCommand(
                execution_id=submitted.id,
                idempotency_key=f"dynamic-continue-error-{unique}",
                expected_version=second.version,
                step=StepSpec(
                    sequence=2,
                    code="raise ValueError('planned dynamic failure')",
                    execution_plan_id="revision-3",
                    plan_step_id="revision-3-step-2",
                    tool_name="failing_tool",
                ),
            )
        )
        failed = await _wait_for(container, failed.id, ExecutionStatus.WAITING_FOR_NEXT_STEP)
        if failed.steps[-1].status != StepStatus.FAILED:
            raise RuntimeError("Cell error did not return to dynamic waiting state.")

        corrected = await container.execution_service.continue_execution(
            ContinueExecutionCommand(
                execution_id=submitted.id,
                idempotency_key=f"dynamic-continue-corrected-{unique}",
                expected_version=failed.version,
                step=StepSpec(
                    sequence=3,
                    code="corrected = answer * 2\nprint(corrected)",
                    execution_plan_id="revision-4",
                    plan_step_id="revision-4-step-3",
                    tool_name="corrected_tool",
                ),
            )
        )
        corrected = await _wait_for(container, corrected.id, ExecutionStatus.WAITING_FOR_NEXT_STEP)
        finishing = await container.execution_service.finish_execution(
            FinishExecutionCommand(
                execution_id=submitted.id,
                idempotency_key=f"dynamic-finish-{unique}",
                expected_version=corrected.version,
            )
        )
        finished = await _wait_for(container, finishing.id, ExecutionStatus.SUCCEEDED)
        attempts = await container.execution_queries.attempts(submitted.id)
        events = await container.execution_queries.events(submitted.id)
        if finished.notebook_path is None:
            raise RuntimeError("Dynamic execution did not persist a notebook path.")
        notebook = settings.workspace_host_root / Path(finished.notebook_path)
        notebook_data = json.loads(notebook.read_text(encoding="utf-8"))
        notebook_text = json.dumps(notebook_data)
        event_types = {event.event_type for event in events}
        if len(attempts) != 1 or len(notebook_data["cells"]) != 4:
            raise RuntimeError("Dynamic Attempt or notebook history is incomplete.")
        if not all(marker in notebook_text for marker in ("42", "84", "planned dynamic failure")):
            raise RuntimeError("Dynamic notebook does not prove same-kernel state continuity.")
        if not {
            "execution.step_completed",
            "execution.step_failed",
            "execution.succeeded",
        }.issubset(event_types):
            raise RuntimeError(f"Dynamic Outbox event history is incomplete: {event_types}")
        if finished.runtime_session_id is not None:
            raise RuntimeError("Finished dynamic execution retained its kernel.")

        print("execution_id:", submitted.id)
        print("retained_runtime_session:", runtime_session_id)
        print("status:", finished.status.value)
        print("attempts:", len(attempts))
        print("step_statuses:", [step.status.value for step in finished.steps])
        print("notebook:", notebook)
    finally:
        await container.stop()


if __name__ == "__main__":
    asyncio.run(main())
