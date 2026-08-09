"""Verify that an expired DYNAMIC wait reclaims a real Jupyter kernel exactly once."""

import asyncio
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select, update

from executor_service.application.commands import StepSpec, SubmitExecutionCommand
from executor_service.config import get_settings
from executor_service.container import ApplicationContainer
from executor_service.domain.enums import (
    CodeSourceType,
    ExecutionMode,
    ExecutionStatus,
    FailureType,
    JupyterPool,
    KernelCleanupStatus,
    TriggerType,
)
from executor_service.domain.models import Execution, utc_now
from executor_service.infrastructure.db.models import (
    ExecutionORM,
    JupyterServerORM,
    OutboxEventORM,
)
from executor_service.infrastructure.jupyter import JupyterGateway


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
    code = "lifecycle_value = 42\nprint(lifecycle_value)"
    container = ApplicationContainer(get_settings())
    await container.start()
    try:
        submitted = await container.execution_service.submit(
            SubmitExecutionCommand(
                idempotency_key=f"dynamic-lifecycle-{unique}",
                mode=ExecutionMode.DYNAMIC,
                trigger_type=TriggerType.INTERACTIVE,
                jupyter_pool=JupyterPool.INTERACTIVE,
                kernel_name="python3",
                code_source_type=CodeSourceType.INLINE,
                code=code,
                code_path=None,
                requested_by_user_id="dynamic-lifecycle-user",
                project_id="dynamic-lifecycle-project",
                session_id="dynamic-lifecycle-session",
                execution_plan_id=f"dynamic-lifecycle-plan-{unique}",
                steps=(StepSpec(sequence=0, code=code, tool_name="initialize"),),
            )
        )
        waiting = await _wait_for(
            container, submitted.id, ExecutionStatus.WAITING_FOR_NEXT_STEP
        )
        if waiting.kernel_id is None or waiting.jupyter_server_id is None:
            raise RuntimeError("Dynamic execution did not retain its assigned kernel.")
        retained_kernel = waiting.kernel_id
        server_id = waiting.jupyter_server_id

        async with container.session_factory() as session, session.begin():
            await session.execute(
                update(ExecutionORM)
                .where(ExecutionORM.id == submitted.id)
                .values(dynamic_wait_expires_at=utc_now() - timedelta(seconds=1))
            )
        await container.execution_worker._audit_dynamic_lifecycle()
        failed = await _wait_for(container, submitted.id, ExecutionStatus.FAILED)
        await container.execution_worker._audit_dynamic_lifecycle()

        async with container.session_factory() as session:
            server = await session.get(JupyterServerORM, server_id)
            failed_events = await session.scalar(
                select(func.count(OutboxEventORM.id)).where(
                    OutboxEventORM.aggregate_id == submitted.id,
                    OutboxEventORM.event_type == "execution.failed",
                )
            )
        if server is None:
            raise RuntimeError("Assigned Jupyter server history was lost.")
        gateway = JupyterGateway(
            server.endpoint,
            container.jupyter_registry.resolve_token(
                server.credential_ref, server.credential_ciphertext
            ),
            container.settings.jupyter_request_timeout_seconds,
        )
        try:
            kernel_exists = await gateway.kernel_exists(retained_kernel)
        finally:
            await gateway.close()
        if (
            failed.failure_type != FailureType.DYNAMIC_WAIT_TIMEOUT
            or failed.kernel_cleanup_status != KernelCleanupStatus.SUCCEEDED
            or failed.kernel_id is not None
            or kernel_exists
            or failed_events != 1
        ):
            raise RuntimeError(f"Dynamic lifecycle cleanup is incomplete: {failed}")

        print("execution_id:", submitted.id)
        print("retained_kernel:", retained_kernel)
        print("failure_type:", failed.failure_type.value)
        print("kernel_cleanup_status:", failed.kernel_cleanup_status.value)
        print("failed_events:", failed_events)
        print("kernel_exists_after_cleanup:", kernel_exists)
    finally:
        await container.stop()


if __name__ == "__main__":
    asyncio.run(main())
