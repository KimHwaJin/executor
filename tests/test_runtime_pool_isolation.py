from pathlib import Path
from uuid import uuid4

from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.commands import StepSpec, SubmitExecutionCommand
from executor_service.application.services import ExecutionService
from executor_service.config import Settings
from executor_service.domain.enums import (
    AttemptStatus,
    CodeSourceType,
    ExecutionMode,
    ExecutionStatus,
    RuntimePool,
    RuntimeTargetStatus,
    TriggerType,
)
from executor_service.domain.models import utc_now
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.runtime_registry import RuntimeTargetRegistry
from executor_service.infrastructure.worker import ExecutionWorker


def _command(pool: RuntimePool, name: str) -> SubmitExecutionCommand:
    return SubmitExecutionCommand(
        idempotency_key=f"pool-{name}-{uuid4().hex}",
        mode=ExecutionMode.STATIC,
        trigger_type=(TriggerType.BATCH if pool == RuntimePool.BATCH else TriggerType.INTERACTIVE),
        runtime_profile="python3",
        code_source_type=CodeSourceType.INLINE,
        source_content=f"print('{name}')",
        code_path=None,
        source_sha256="0" * 64,
        user_id="pool-user",
        project_id="pool-project",
        session_id=f"pool-session-{name}",
        task_id="test-task",
        execution_plan_id=f"pool-plan-{name}",
        steps=(
            StepSpec(
                sequence=0,
                code=f"print('{name}')",
                execution_plan_id=f"pool-plan-{name}",
                plan_step_id=f"pool-plan-{name}-step-0",
                tool_name=name,
            ),
        ),
    )


def _server(
    name: str,
    pool: RuntimePool,
    *,
    status: RuntimeTargetStatus = RuntimeTargetStatus.ACTIVE,
    capacity: int = 1,
) -> RuntimeTargetORM:
    return RuntimeTargetORM(
        name=name,
        connection_config={"endpoint": f"http://{name}.invalid:8888"},
        credential_ref="settings:JUPYTER_TOKEN",
        pool=pool,
        status=status,
        max_concurrent_executions=capacity,
        supported_profiles=["python3"],
        enabled=True,
    )


def _worker(engine: AsyncEngine, tmp_path: Path, consumer: str) -> tuple[ExecutionWorker, Redis]:
    settings = Settings(
        runtime_enabled=False,
        workspace_host_root=tmp_path,
        execution_consumer_name=consumer,
    )
    session_factory = create_session_factory(engine)
    redis = Redis.from_url("redis://127.0.0.1:6379/15", decode_responses=True)
    return (
        ExecutionWorker(
            session_factory=session_factory,
            redis=redis,
            settings=settings,
            registry=RuntimeTargetRegistry(session_factory, settings),
            artifact_manager=ExecutionArtifactManager(session_factory, settings),
        ),
        redis,
    )


async def test_interactive_and_batch_claims_are_isolated_and_batch_uses_two_servers(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        session.add_all(
            [
                _server("interactive-a", RuntimePool.INTERACTIVE),
                _server("batch-a", RuntimePool.BATCH),
                _server("batch-b", RuntimePool.BATCH),
            ]
        )
    interactive = await execution_service.submit(_command(RuntimePool.INTERACTIVE, "interactive"))
    first_batch = await execution_service.submit(_command(RuntimePool.BATCH, "batch-one"))
    second_batch = await execution_service.submit(_command(RuntimePool.BATCH, "batch-two"))
    worker, redis = _worker(engine, tmp_path, "pool-worker")
    try:
        interactive_claim = await worker._claim(interactive.id)
        first_batch_claim = await worker._claim(first_batch.id)
        second_batch_claim = await worker._claim(second_batch.id)
    finally:
        await redis.aclose()

    assert interactive_claim is not None
    assert first_batch_claim is not None
    assert second_batch_claim is not None
    assert interactive_claim[1].pool == RuntimePool.INTERACTIVE
    assert {first_batch_claim[1].name, second_batch_claim[1].name} == {
        "batch-a",
        "batch-b",
    }
    assert first_batch_claim[1].pool == RuntimePool.BATCH
    assert second_batch_claim[1].pool == RuntimePool.BATCH


async def test_full_batch_pool_keeps_work_queued_until_capacity_is_released(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        session.add_all(
            [
                _server("interactive-spare", RuntimePool.INTERACTIVE, capacity=10),
                _server("batch-capacity-a", RuntimePool.BATCH),
                _server("batch-capacity-b", RuntimePool.BATCH),
            ]
        )
    first = await execution_service.submit(_command(RuntimePool.BATCH, "capacity-one"))
    second = await execution_service.submit(_command(RuntimePool.BATCH, "capacity-two"))
    waiting = await execution_service.submit(_command(RuntimePool.BATCH, "capacity-waiting"))
    worker, redis = _worker(engine, tmp_path, "capacity-worker")
    try:
        first_claim = await worker._claim(first.id)
        second_claim = await worker._claim(second.id)
        assert first_claim is not None and second_claim is not None
        assert await worker._claim(waiting.id) is None
        assert (await execution_service.get(waiting.id)).status == ExecutionStatus.QUEUED

        async with session_factory() as session, session.begin():
            first_attempt_id = await session.scalar(
                select(ExecutionAttemptORM.id).where(ExecutionAttemptORM.execution_id == first.id)
            )
            assert first_attempt_id is not None
            await session.execute(
                update(ExecutionAttemptORM)
                .where(ExecutionAttemptORM.id == first_attempt_id)
                .values(status=AttemptStatus.SUCCEEDED, finished_at=utc_now())
            )
            await session.execute(
                update(ExecutionORM)
                .where(ExecutionORM.id == first.id)
                .values(status=ExecutionStatus.SUCCEEDED, finished_at=utc_now())
            )

        waiting_claim = await worker._claim(waiting.id)
    finally:
        await redis.aclose()

    assert waiting_claim is not None
    assert waiting_claim[1].pool == RuntimePool.BATCH
    assert waiting_claim[1].name == first_claim[1].name


async def test_batch_never_falls_back_to_interactive_or_draining_server(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        session.add_all(
            [
                _server("interactive-only", RuntimePool.INTERACTIVE, capacity=10),
                _server(
                    "batch-draining",
                    RuntimePool.BATCH,
                    status=RuntimeTargetStatus.DRAINING,
                    capacity=10,
                ),
            ]
        )
    batch = await execution_service.submit(_command(RuntimePool.BATCH, "no-fallback"))
    worker, redis = _worker(engine, tmp_path, "no-fallback-worker")
    try:
        assert await worker._claim(batch.id) is None
    finally:
        await redis.aclose()
    assert (await execution_service.get(batch.id)).status == ExecutionStatus.QUEUED


async def test_queued_batch_claims_a_server_added_during_scale_up(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        session.add(_server("scale-interactive", RuntimePool.INTERACTIVE, capacity=10))
    batch = await execution_service.submit(_command(RuntimePool.BATCH, "scale-up"))
    worker, redis = _worker(engine, tmp_path, "scale-up-worker")
    try:
        assert await worker._claim(batch.id) is None
        async with session_factory() as session, session.begin():
            session.add(_server("scale-batch-new", RuntimePool.BATCH))
        claim = await worker._claim(batch.id)
    finally:
        await redis.aclose()

    assert claim is not None
    assert claim[1].name == "scale-batch-new"
    assert claim[1].pool == RuntimePool.BATCH
