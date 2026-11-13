from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.commands import (
    StepSpec,
    SubmitExecutionCommand,
)
from executor_service.application.execution_queries import StatusReason
from executor_service.application.services import ExecutionService
from executor_service.domain.enums import (
    AttemptStatus,
    ExecutionStatus,
    ExecutorAdmissionState,
    FailureType,
    FinalizedBy,
    HandoverReason,
    OperationMode,
    RuntimePool,
    RuntimeTargetStatus,
    StatusReasonCode,
    TriggerType,
)
from executor_service.domain.models import utc_now
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionHandoverORM,
    ExecutionORM,
    ExecutorMaintenanceORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.execution_queries import (
    SQLAlchemyExecutionQueryService,
)
from executor_service.interfaces.contracts import ExecutionResponse
from executor_service.settings import Settings
from tests.runtime_credentials import runtime_credential_fields


def _command(key: str) -> SubmitExecutionCommand:
    return SubmitExecutionCommand(
        idempotency_key=key,
        operation_mode=OperationMode.SINGLE,
        trigger_type=TriggerType.INTERACTIVE,
        runtime_profile="basic",
        user_id="reason-user",
        project_id=None,
        session_id=None,
        tenant_id="test-tenant",
        steps=(StepSpec(sequence=0, code="print(1)"),),
    )


def _target(**values: Any) -> RuntimeTargetORM:
    return RuntimeTargetORM(
        **{
            "id": uuid4(),
            "name": "jupyter-a",
            "connection_config": {"endpoint": "http://127.0.0.1:8888"},
            **runtime_credential_fields(),
            "pool": RuntimePool.INTERACTIVE,
            "status": RuntimeTargetStatus.ACTIVE,
            "max_concurrent_executions": 1,
            "supported_profiles": ["basic"],
            "enabled": True,
            **values,
        }
    )


def _queries(engine: AsyncEngine) -> SQLAlchemyExecutionQueryService:
    return SQLAlchemyExecutionQueryService(
        create_session_factory(engine),
        Settings(_env_file=None, auth_clients=[]),
    )


async def _set(engine: AsyncEngine, execution_id: UUID, **values: Any) -> None:
    async with create_session_factory(engine)() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution_id)
            .values(**values)
        )


async def test_queued_reason_follows_what_blocks_the_claim(
    execution_service: ExecutionService, engine: AsyncEngine
) -> None:
    first = await execution_service.submit(_command("first"))
    second = await execution_service.submit(_command("second"))
    queries = _queries(engine)
    factory = create_session_factory(engine)

    async def reason() -> StatusReason | None:
        return (await queries.execution(second.id)).reason

    assert await reason() == StatusReason(
        StatusReasonCode.NO_ACTIVE_TARGET,
        "No enabled, ACTIVE Runtime Target serves this pool.",
        {"queue_position": 2},
    )

    target = _target(supported_profiles=["ml"])
    async with factory() as session, session.begin():
        session.add(target)
    found = await reason()
    assert found and found.code == StatusReasonCode.PROFILE_UNSUPPORTED

    async with factory() as session, session.begin():
        await session.execute(
            update(RuntimeTargetORM).values(supported_profiles=["basic"])
        )
    found = await reason()
    assert found and found.code == StatusReasonCode.WAITING_FOR_WORKER

    now = utc_now()
    async with factory() as session, session.begin():
        session.add(
            ExecutionAttemptORM(
                id=uuid4(),
                execution_id=first.id,
                attempt_number=1,
                runtime_target_id=target.id,
                status=AttemptStatus.RUNNING,
                heartbeat_at=now,
                started_at=now,
            )
        )
    await _set(engine, first.id, status=ExecutionStatus.RUNNING)
    found = await reason()
    assert found and found.code == StatusReasonCode.CAPACITY_FULL
    assert found.detail == {
        "queue_position": 1,
        "targets": {"jupyter-a": "1/1"},
    }
    page = await queries.executions(status=ExecutionStatus.QUEUED)
    assert [item.reason for item in page.items] == [found]

    async with factory() as session, session.begin():
        session.add(
            ExecutorMaintenanceORM(
                admission_state=ExecutorAdmissionState.DRAINING
            )
        )
    found = await reason()
    assert found and found.code == StatusReasonCode.ADMISSION_DRAINING


async def test_queued_run_that_kept_its_kernel_waits_for_that_target(
    execution_service: ExecutionService, engine: AsyncEngine
) -> None:
    execution = await execution_service.submit(_command("pinned"))
    target = _target(status=RuntimeTargetStatus.OFFLINE)
    async with create_session_factory(engine)() as session, session.begin():
        session.add(target)
    await _set(
        engine,
        execution.id,
        runtime_target_id=target.id,
        runtime_session_id="kernel-1",
        # A deferred retained retry keeps its last failure on the row.
        failure_type=FailureType.RUNTIME_UNAVAILABLE,
        error_message="waiting for recovery",
    )

    reason = (await _queries(engine).execution(execution.id)).reason

    assert reason and reason.code == StatusReasonCode.TARGET_UNAVAILABLE
    assert reason.detail == {
        "queue_position": 1,
        "targets": {"jupyter-a": "OFFLINE"},
    }


async def test_running_has_a_reason_only_while_handed_over(
    execution_service: ExecutionService, engine: AsyncEngine
) -> None:
    execution = await execution_service.submit(_command("running"))
    await _set(engine, execution.id, status=ExecutionStatus.RUNNING)
    queries = _queries(engine)
    assert (await queries.execution(execution.id)).reason is None

    paused_since = utc_now()
    async with create_session_factory(engine)() as session, session.begin():
        session.add(
            ExecutionHandoverORM(
                execution_id=execution.id,
                reason=HandoverReason.EXECUTOR_UNRESPONSIVE,
                paused_since=paused_since,
            )
        )

    reason = (await queries.execution(execution.id)).reason
    assert reason and reason.code == StatusReasonCode.WORKER_HANDOVER
    assert reason.detail == {
        "handover_reason": "EXECUTOR_UNRESPONSIVE",
        "paused_since": paused_since,
    }


@pytest.mark.parametrize(
    ("values", "code", "message"),
    [
        (
            {
                "status": ExecutionStatus.FAILED,
                "failure_type": FailureType.QUEUE_TIMEOUT,
                "error_message": "No Runtime Target accepted it.",
            },
            FailureType.QUEUE_TIMEOUT,
            "No Runtime Target accepted it.",
        ),
        (
            {
                "status": ExecutionStatus.CANCELLED,
                "cancellation_reason": "user stopped",
            },
            StatusReasonCode.CANCELLED_BY_REQUEST,
            "user stopped",
        ),
        (
            {
                "status": ExecutionStatus.CANCEL_REQUESTED,
                "finalized_by": FinalizedBy.MAX_RUNTIME,
                "cancellation_reason": "Execution exceeded its maximum runtime.",
            },
            FinalizedBy.MAX_RUNTIME,
            "Execution exceeded its maximum runtime.",
        ),
        (
            {
                "status": ExecutionStatus.FINALIZING,
                "finalized_by": FinalizedBy.WAIT_TIMEOUT,
            },
            FinalizedBy.WAIT_TIMEOUT,
            "No next Operation arrived before the wait deadline.",
        ),
        (
            {"status": ExecutionStatus.SUCCEEDED},
            StatusReasonCode.COMPLETED,
            "Every Step completed.",
        ),
        (
            {"status": ExecutionStatus.WAITING_FOR_OPERATION},
            StatusReasonCode.AWAITING_NEXT_OPERATION,
            "Waiting for the next Operation or a finalize call.",
        ),
    ],
)
async def test_other_statuses_report_the_reason_the_row_records(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    values: dict[str, Any],
    code: object,
    message: str,
) -> None:
    execution = await execution_service.submit(_command("stored"))
    await _set(engine, execution.id, **values)

    view = await _queries(engine).execution(execution.id)

    assert view.reason == StatusReason(code, message)  # type: ignore[arg-type]
    state = ExecutionResponse.from_view(view).model_dump(mode="json")["state"]
    assert state == {
        "status": values["status"].value,
        "version": view.version,
        "reason": {"code": str(code), "message": message, "detail": None},
    }
