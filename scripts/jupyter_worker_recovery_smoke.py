"""Verify graceful Worker shutdown and a FROM_START retry against real Jupyter."""

import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

from executor_service.application.commands import (
    RetryExecutionCommand,
    StepSpec,
    SubmitExecutionCommand,
)
from executor_service.config import get_settings
from executor_service.container import ApplicationContainer
from executor_service.domain.enums import (
    CodeSourceType,
    ExecutionMode,
    ExecutionStatus,
    FailureType,
    RetryStrategy,
    RuntimeSessionCleanupStatus,
    TriggerType,
)


async def _wait(
    container: ApplicationContainer,
    execution_id: Any,
    statuses: set[ExecutionStatus],
    *,
    require_kernel: bool = False,
) -> Any:
    for _ in range(300):
        execution = await container.execution_service.get(execution_id)
        if execution.status in statuses and (
            not require_kernel or execution.runtime_session_id is not None
        ):
            return execution
        await asyncio.sleep(0.2)
    raise RuntimeError(f"Execution {execution_id} did not reach {statuses}.")


async def main() -> None:
    unique = str(uuid4())
    marker_relative = Path("checkpoints") / f"worker-recovery-{unique}.marker"
    code = (
        "from pathlib import Path\n"
        "import time\n"
        f"marker = Path('{marker_relative.as_posix()}')\n"
        "if not marker.exists():\n"
        "    marker.write_text('started', encoding='utf-8')\n"
        "    time.sleep(60)\n"
        "print('worker recovery completed')\n"
    )
    settings = get_settings()
    first = ApplicationContainer(settings)
    second: ApplicationContainer | None = None
    await first.start()
    try:
        submitted = await first.execution_service.submit(
            SubmitExecutionCommand(
                idempotency_key=f"worker-recovery-submit-{unique}",
                mode=ExecutionMode.STATIC,
                trigger_type=TriggerType.INTERACTIVE,
                runtime_profile="python3",
                code_source_type=CodeSourceType.INLINE,
                source_content=code,
                code_path=None,
                source_sha256="0" * 64,
                requested_by_user_id="worker-recovery-user",
                project_id="worker-recovery-project",
                session_id="worker-recovery-session",
                task_id="test-task",
                execution_plan_id=f"worker-recovery-plan-{unique}",
                steps=(
                    StepSpec(
                        sequence=0,
                        code=code,
                        execution_plan_id=f"worker-recovery-plan-{unique}",
                        plan_step_id=f"worker-recovery-plan-{unique}-step-0",
                        tool_name="long_running_tool",
                    ),
                ),
            )
        )
        running = await _wait(
            first,
            submitted.id,
            {ExecutionStatus.RUNNING},
            require_kernel=True,
        )
        original_kernel = running.runtime_session_id
    finally:
        await first.stop()

    second = ApplicationContainer(settings)
    await second.start()
    try:
        failed = await _wait(second, submitted.id, {ExecutionStatus.FAILED})
        if (
            failed.failure_type != FailureType.WORKER_SHUTDOWN
            or failed.retry_strategy != RetryStrategy.FROM_START
            or failed.retry_from_sequence != 0
            or failed.runtime_session_cleanup_status != RuntimeSessionCleanupStatus.SUCCEEDED
            or failed.runtime_session_id is not None
        ):
            raise RuntimeError(f"Worker shutdown was not classified safely: {failed}")

        await second.execution_service.retry(
            RetryExecutionCommand(
                execution_id=submitted.id,
                idempotency_key=f"worker-recovery-retry-{unique}",
            )
        )
        succeeded = await _wait(
            second,
            submitted.id,
            {ExecutionStatus.SUCCEEDED, ExecutionStatus.FAILED},
        )
        if succeeded.status != ExecutionStatus.SUCCEEDED:
            raise RuntimeError(f"FROM_START retry failed: {succeeded}")
        attempts = await second.execution_queries.attempts(submitted.id)
        if (
            len(attempts) != 2
            or attempts[0].failure_type != FailureType.WORKER_SHUTDOWN
            or attempts[0].retry_strategy != RetryStrategy.FROM_START
            or attempts[0].runtime_session_cleanup_status != RuntimeSessionCleanupStatus.SUCCEEDED
        ):
            raise RuntimeError(f"Recovery Attempt history is incomplete: {attempts}")
        print("execution_id:", submitted.id)
        print("initial_kernel:", original_kernel)
        print("failure_type:", failed.failure_type.value)
        print("retry_strategy:", failed.retry_strategy.value)
        print("runtime_session_cleanup_status:", failed.runtime_session_cleanup_status.value)
        print("retry_status:", succeeded.status.value)
        print("attempts:", len(attempts))
    finally:
        await second.stop()


if __name__ == "__main__":
    asyncio.run(main())
