"""A running Step moves to another Worker without re-running its code."""

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from typing import Any, ClassVar, cast
from uuid import UUID

import pytest
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.commands import (
    CancelExecutionCommand,
    StepSpec,
    SubmitExecutionCommand,
)
from executor_service.application.services import ExecutionService
from executor_service.domain.enums import (
    ExecutionStatus,
    FailureType,
    HandoverReason,
    OperationMode,
    RuntimeAbortStatus,
    RuntimePool,
    RuntimeTargetStatus,
    RuntimeType,
    StepStatus,
    TriggerType,
)
from executor_service.domain.models import utc_now
from executor_service.domain.runtime import (
    RuntimeAbortResult,
    RuntimeCatchUpHandler,
    RuntimeDriverFactory,
    RuntimeExecutionResult,
    RuntimeOutputHandler,
    RuntimeSessionLostError,
)
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionEventORM,
    ExecutionHandoverORM,
    ExecutionORM,
    ExecutionStepORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.execution_worker import ExecutionWorker
from executor_service.infrastructure.runtime_registry import (
    RuntimeTargetRegistry,
)
from executor_service.settings import Settings
from tests.runtime_credentials import runtime_credential_fields
from tests.runtime_storage_fake import InMemoryRuntimeStorage, stream_record


class JournalKernel(InMemoryRuntimeStorage):
    """A kernel that keeps running whichever Worker follows its journal.

    Each Step prints ``before`` at once and ``after`` when ``release`` is
    set. Following replays from the start, like the Jupyter extension.
    """

    submissions: ClassVar[list[str]] = []
    started: ClassVar[asyncio.Event]
    release: ClassVar[asyncio.Event]
    lost: ClassVar[bool] = False
    deleted: ClassVar[list[str]] = []

    @classmethod
    def configure(cls) -> None:
        cls.reset_storage()
        cls.submissions = []
        cls.started = asyncio.Event()
        cls.release = asyncio.Event()
        cls.lost = False
        cls.deleted = []

    async def start_session(self, *_args: object) -> str:
        return "journal-kernel"

    async def start_execution(
        self, session_id: str, request_id: UUID, code: str
    ) -> None:
        if request_id not in type(self).submitted:
            type(self).submissions.append(code)
        await super().start_execution(session_id, request_id, code)

    async def follow_execution(
        self,
        session_id: str,
        request_id: UUID,
        output_handler: RuntimeOutputHandler,
        on_caught_up: RuntimeCatchUpHandler | None = None,
    ) -> RuntimeExecutionResult:
        del session_id
        if type(self).lost:
            raise RuntimeSessionLostError("The kernel process stopped.")
        code = type(self).submitted[request_id]
        await output_handler(stream_record(f"{code} before\n"))
        if on_caught_up is not None:
            await on_caught_up(type(self).release.is_set())
        type(self).started.set()
        await type(self).release.wait()
        await output_handler(stream_record(f"{code} after\n"))
        return RuntimeExecutionResult(execution_count=1)

    async def abort_session(
        self, _session_id: str, _timeout_seconds: float
    ) -> RuntimeAbortResult:
        return RuntimeAbortResult(RuntimeAbortStatus.IDLE_CONFIRMED)

    async def interrupt_session(self, _session_id: str) -> None:
        pass

    async def delete_session(self, session_id: str) -> None:
        type(self).deleted.append(session_id)

    async def close(self) -> None:
        pass


class JournalKernelFactory:
    def create(
        self,
        _runtime_type: RuntimeType,
        _connection_config: dict[str, Any],
        _credential: str,
    ) -> Any:
        return JournalKernel()


def _worker(
    engine: AsyncEngine, tmp_path: Path, name: str, redis: Redis
) -> ExecutionWorker:
    factory = create_session_factory(engine)
    settings = Settings(
        runtime_enabled=False,
        shared_storage_root=tmp_path,
    )
    return ExecutionWorker(
        session_factory=factory,
        settings=settings,
        registry=RuntimeTargetRegistry(factory, settings),
        artifact_manager=ExecutionArtifactManager(factory),
        driver_factory=cast(RuntimeDriverFactory, JournalKernelFactory()),
    )


async def _submit(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    mode: OperationMode,
) -> UUID:
    async with create_session_factory(engine)() as session, session.begin():
        session.add(
            RuntimeTargetORM(
                name="journal-runtime",
                connection_config={"endpoint": "http://runtime.invalid"},
                **runtime_credential_fields(),
                pool=RuntimePool.INTERACTIVE,
                status=RuntimeTargetStatus.ACTIVE,
                max_concurrent_executions=1,
                supported_profiles=["basic"],
                enabled=True,
            )
        )
    execution = await execution_service.submit(
        SubmitExecutionCommand(
            idempotency_key=f"handover-{mode.value}",
            operation_mode=mode,
            operation_wait_timeout_seconds=600
            if mode == OperationMode.MULTI
            else None,
            trigger_type=TriggerType.INTERACTIVE,
            runtime_profile="basic",
            user_id="handover-user",
            project_id=None,
            session_id=None,
            task_id="handover-task",
            steps=(
                StepSpec(sequence=0, code="first"),
                StepSpec(sequence=1, code="second"),
            ),
        )
    )
    return execution.id


async def _event_payloads(
    engine: AsyncEngine, execution_id: UUID, event_type: str
) -> list[dict[str, Any]]:
    async with create_session_factory(engine)() as session:
        return list(
            await session.scalars(
                select(ExecutionEventORM.payload)
                .where(
                    ExecutionEventORM.execution_id == execution_id,
                    ExecutionEventORM.event_type == event_type,
                )
                .order_by(ExecutionEventORM.event_sequence)
            )
        )


async def _step_text(
    engine: AsyncEngine, tmp_path: Path, sequence: int
) -> str:
    async with create_session_factory(engine)() as session:
        step = await session.scalar(
            select(ExecutionStepORM).where(
                ExecutionStepORM.sequence == sequence
            )
        )
    assert step is not None and step.result_manifest_path is not None
    manifest = json.loads((tmp_path / step.result_manifest_path).read_bytes())
    return "".join(
        (tmp_path / output["representations"][0]["relative_path"]).read_text()
        for output in manifest["outputs"]
    )


@pytest.fixture(autouse=True)
def _configure_kernel() -> None:
    JournalKernel.configure()


@pytest.mark.parametrize("mode", [OperationMode.SINGLE, OperationMode.MULTI])
async def test_shutdown_hands_the_running_step_to_another_worker(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    mode: OperationMode,
) -> None:
    execution_id = await _submit(execution_service, engine, mode)
    redis = Redis.from_url("redis://127.0.0.1:6379/15", decode_responses=True)
    try:
        stopping = _worker(engine, tmp_path, "worker-a", redis)
        run = asyncio.create_task(stopping._runner.run(execution_id))
        await asyncio.wait_for(JournalKernel.started.wait(), 10)
        stopping._step_executor.set_shutting_down(True)
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)

        async with create_session_factory(engine)() as session:
            released = await session.get(ExecutionORM, execution_id)
            handover = await session.get(ExecutionHandoverORM, execution_id)
        assert released is not None
        assert released.status == ExecutionStatus.RUNNING
        assert released.lease_owner is None
        assert handover is not None
        assert handover.reason == HandoverReason.EXECUTOR_SHUTDOWN
        assert JournalKernel.deleted == []
        [started] = await _event_payloads(
            engine, execution_id, "execution.step_handover_started"
        )
        assert started["reason"] == "EXECUTOR_SHUTDOWN"
        assert started["kernel_running"] is True
        assert started["step"]["sequence"] == 0

        # The Step finishes while nobody follows it.
        JournalKernel.started = asyncio.Event()
        JournalKernel.release.set()
        await _worker(engine, tmp_path, "worker-b", redis)._runner.run(
            execution_id
        )
    finally:
        await redis.aclose()

    finished = await execution_service.get(execution_id)
    assert finished.status == (
        ExecutionStatus.SUCCEEDED
        if mode == OperationMode.SINGLE
        else ExecutionStatus.WAITING_FOR_OPERATION
    )
    assert [step.status for step in finished.steps] == [
        StepStatus.SUCCEEDED,
        StepStatus.SUCCEEDED,
    ]
    assert JournalKernel.submissions == ["first", "second"]
    assert (
        await _step_text(engine, tmp_path, 0) == "first before\nfirst after\n"
    )
    [completed] = await _event_payloads(
        engine, execution_id, "execution.step_handover_completed"
    )
    assert completed["finished_during_handover"] is True
    assert completed["outputs_complete"] is True
    assert completed["step"]["sequence"] == 0
    step_events = await _event_payloads(
        engine, execution_id, "execution.step_started"
    )
    assert [event["step"]["sequence"] for event in step_events] == [0, 1]
    async with create_session_factory(engine)() as session:
        assert await session.get(ExecutionHandoverORM, execution_id) is None


async def _expire_lease(engine: AsyncEngine, execution_id: UUID) -> None:
    past = utc_now() - timedelta(seconds=5)
    async with create_session_factory(engine)() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution_id)
            .values(lease_expires_at=past)
        )
        await session.execute(
            update(ExecutionAttemptORM)
            .where(ExecutionAttemptORM.execution_id == execution_id)
            .values(lease_expires_at=past)
        )


async def test_unresponsive_worker_is_replaced_and_fenced_out(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    execution_id = await _submit(
        execution_service, engine, OperationMode.SINGLE
    )
    redis = Redis.from_url("redis://127.0.0.1:6379/15", decode_responses=True)
    try:
        frozen = _worker(engine, tmp_path, "worker-a", redis)
        stale = asyncio.create_task(frozen._runner.run(execution_id))
        await asyncio.wait_for(JournalKernel.started.wait(), 10)
        await _expire_lease(engine, execution_id)

        replacement = _worker(engine, tmp_path, "worker-b", redis)
        assert await replacement._lease_recovery.recover() == 1
        [started] = await _event_payloads(
            engine, execution_id, "execution.step_handover_started"
        )
        assert started["reason"] == "EXECUTOR_UNRESPONSIVE"

        JournalKernel.started = asyncio.Event()
        follow = asyncio.create_task(replacement._runner.run(execution_id))
        await asyncio.wait_for(JournalKernel.started.wait(), 10)
        [completed] = await _event_payloads(
            engine, execution_id, "execution.step_handover_completed"
        )
        assert completed["finished_during_handover"] is False

        # The stale Worker wakes up but can no longer commit or clean up.
        JournalKernel.release.set()
        await asyncio.wait_for(asyncio.gather(stale, follow), 10)
    finally:
        await redis.aclose()

    finished = await execution_service.get(execution_id)
    assert finished.status == ExecutionStatus.SUCCEEDED
    assert JournalKernel.submissions == ["first", "second"]
    assert JournalKernel.deleted == ["journal-kernel"]
    assert (
        await _step_text(engine, tmp_path, 0) == "first before\nfirst after\n"
    )
    completed_steps = await _event_payloads(
        engine, execution_id, "execution.step_completed"
    )
    assert [event["step"]["sequence"] for event in completed_steps] == [0, 1]


async def test_a_lost_kernel_fails_the_handed_over_step(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    execution_id = await _submit(
        execution_service, engine, OperationMode.SINGLE
    )
    redis = Redis.from_url("redis://127.0.0.1:6379/15", decode_responses=True)
    try:
        stopping = _worker(engine, tmp_path, "worker-a", redis)
        run = asyncio.create_task(stopping._runner.run(execution_id))
        await asyncio.wait_for(JournalKernel.started.wait(), 10)
        stopping._step_executor.set_shutting_down(True)
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)

        JournalKernel.lost = True
        await _worker(engine, tmp_path, "worker-b", redis)._runner.run(
            execution_id
        )
    finally:
        await redis.aclose()

    finished = await execution_service.get(execution_id)
    assert finished.status == ExecutionStatus.FAILED
    assert finished.failure_type == FailureType.RUNTIME_SESSION_LOST
    assert finished.steps[0].status == StepStatus.FAILED
    assert finished.steps[1].status == StepStatus.SKIPPED
    assert JournalKernel.submissions == ["first"]
    assert (
        await _event_payloads(
            engine, execution_id, "execution.step_handover_completed"
        )
        == []
    )


async def _release_running_step(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    redis: Redis,
) -> UUID:
    execution_id = await _submit(
        execution_service, engine, OperationMode.SINGLE
    )
    stopping = _worker(engine, tmp_path, "worker-a", redis)
    run = asyncio.create_task(stopping._runner.run(execution_id))
    await asyncio.wait_for(JournalKernel.started.wait(), 10)
    stopping._step_executor.set_shutting_down(True)
    run.cancel()
    await asyncio.gather(run, return_exceptions=True)
    return execution_id


async def test_cancelling_a_released_step_clears_its_handover(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    redis = Redis.from_url("redis://127.0.0.1:6379/15", decode_responses=True)
    try:
        execution_id = await _release_running_step(
            execution_service, engine, tmp_path, redis
        )
        await execution_service.cancel(
            CancelExecutionCommand(
                execution_id=execution_id, idempotency_key="cancel-handover"
            )
        )
        await _worker(
            engine, tmp_path, "worker-b", redis
        )._cancellation.cancel(execution_id)
    finally:
        await redis.aclose()

    finished = await execution_service.get(execution_id)
    assert finished.status == ExecutionStatus.CANCELLED
    async with create_session_factory(engine)() as session:
        assert await session.get(ExecutionHandoverORM, execution_id) is None


async def test_a_handover_without_its_target_fails_instead_of_waiting(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    redis = Redis.from_url("redis://127.0.0.1:6379/15", decode_responses=True)
    try:
        execution_id = await _release_running_step(
            execution_service, engine, tmp_path, redis
        )
        async with (
            create_session_factory(engine)() as session,
            session.begin(),
        ):
            # Simulate a Target row that is gone (inconsistent fleet state).
            await session.execute(
                update(ExecutionORM)
                .where(ExecutionORM.id == execution_id)
                .values(runtime_target_id=None)
            )
        replacement = _worker(engine, tmp_path, "worker-b", redis)
        await replacement._runner.run(execution_id)
        assert await replacement._lease_recovery.recover() == 1
    finally:
        await redis.aclose()

    finished = await execution_service.get(execution_id)
    assert finished.status == ExecutionStatus.FAILED
    assert finished.failure_type == FailureType.LEASE_EXPIRED
    async with create_session_factory(engine)() as session:
        assert await session.get(ExecutionHandoverORM, execution_id) is None
