"""Redis-triggered Runtime execution worker with PostgreSQL leases."""

import asyncio
import logging
import os
import socket
from collections.abc import AsyncIterator, Awaitable, Coroutine
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from opentelemetry.trace import SpanKind
from redis.asyncio import Redis
from redis.exceptions import ResponseError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from executor_service.config import Settings
from executor_service.domain.enums import (
    ArtifactStatus,
    AttemptStatus,
    ExecutionStatus,
    FailureType,
    OperationMode,
    OperationStatus,
    RetryStrategy,
    RuntimePool,
    RuntimeSessionCleanupStatus,
    RuntimeTargetStatus,
    StepStatus,
)
from executor_service.domain.models import Execution, utc_now
from executor_service.domain.runtime import (
    RuntimeDriver,
    RuntimeDriverError,
    RuntimeDriverFactory,
    RuntimeExecutionError,
    RuntimeExecutionTimeoutError,
)
from executor_service.events import build_execution_event
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionOperationORM,
    ExecutionORM,
    ExecutionStepAttemptORM,
    ExecutionStepORM,
    OutboxEventORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.runtime_drivers import ConfiguredRuntimeDriverFactory
from executor_service.infrastructure.runtime_registry import RuntimeTargetRegistry
from executor_service.infrastructure.workspace import ExecutionWorkspace, WorkspaceManager
from executor_service.tracing import (
    TracingManager,
    capture_trace_carrier,
    extract_trace_context,
)
from executor_service.work_messages import WORK_MESSAGE_SCHEMA_VERSION, WorkStreamEnvelope

logger = logging.getLogger(__name__)

DISPATCH_MESSAGE_TYPES = frozenset(
    {
        "operation.ready",
        "execution.finalization_ready",
        "execution.retry_ready",
        "execution.cancellation_ready",
    }
)
RUN_MESSAGE_TYPES = DISPATCH_MESSAGE_TYPES - {"execution.cancellation_ready"}


class RetainedRuntimeSessionLostError(RuntimeDriverError):
    """Raised when a retained-session retry reaches its target but the session is gone."""


class ExecutionWorker:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis,
        settings: Settings,
        registry: RuntimeTargetRegistry,
        artifact_manager: ExecutionArtifactManager,
        driver_factory: RuntimeDriverFactory | None = None,
        tracing: TracingManager | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._redis = redis
        self._settings = settings
        self._registry = registry
        self._driver_factory = driver_factory or ConfiguredRuntimeDriverFactory(settings)
        self._artifacts = artifact_manager
        self._tracing = tracing or TracingManager(settings)
        self._workspace = WorkspaceManager()
        self._consumer_name = settings.execution_consumer_name or (
            f"{socket.gethostname()}-{os.getpid()}"
        )
        self._stop_event = asyncio.Event()
        self._admission_loops: list[asyncio.Task[None]] = []
        self._maintenance_loops: list[asyncio.Task[None]] = []
        self._jobs: dict[UUID, asyncio.Task[None]] = {}
        self._jobs_idle = asyncio.Event()
        self._jobs_idle.set()
        self._accepting_work = False
        self._draining = False
        self._stopped = True
        self._pending_claim_cursor = "0-0"

    @property
    def accepting_work(self) -> bool:
        return self._accepting_work

    @property
    def draining(self) -> bool:
        return self._draining

    @property
    def active_job_count(self) -> int:
        return len(self._jobs)

    def _create_driver(self, target: RuntimeTargetORM) -> RuntimeDriver:
        credential = self._registry.resolve_credential(
            target.credential_ref, target.credential_ciphertext
        )
        return self._driver_factory.create(
            target.runtime_type, target.connection_config, credential
        )

    @property
    def lifecycle_state(self) -> str:
        if self._stopped:
            return "STOPPED"
        if self._draining:
            return "DRAINING"
        if self._accepting_work:
            return "ACCEPTING"
        return "STARTING"

    async def start(self) -> None:
        if not self._settings.runtime_enabled or self._admission_loops or self._maintenance_loops:
            return
        await self._ensure_consumer_group()
        self._stop_event.clear()
        self._stopped = False
        self._draining = False
        self._accepting_work = True
        self._admission_loops = [
            asyncio.create_task(self._stream_loop(), name="execution-stream-consumer"),
            asyncio.create_task(
                self._pending_recovery_loop(),
                name="execution-pending-recovery",
            ),
            asyncio.create_task(self._reconcile_loop(), name="execution-reconciler"),
        ]
        self._maintenance_loops = [
            asyncio.create_task(self._lease_recovery_loop(), name="execution-lease-recovery"),
            asyncio.create_task(
                self._retained_runtime_session_cleanup_loop(),
                name="retained-session-cleanup",
            ),
            asyncio.create_task(
                self._multi_lifecycle_loop(),
                name="multi-lifecycle-auditor",
            ),
        ]

    async def begin_drain(self) -> None:
        if self._stopped or self._draining:
            return
        self._accepting_work = False
        self._draining = True
        logger.info(
            "Executor Worker drain started",
            extra={"active_job_count": self.active_job_count},
        )
        await self._cancel_tasks(self._admission_loops)

    async def stop(self) -> None:
        if self._stopped and not self._jobs:
            return
        await self.begin_drain()
        if self._jobs:
            try:
                async with asyncio.timeout(self._settings.execution_drain_timeout_seconds):
                    await self._jobs_idle.wait()
            except TimeoutError:
                logger.warning(
                    "Executor Worker drain deadline exceeded; cancelling remaining jobs",
                    extra={"active_job_count": self.active_job_count},
                )
        self._stop_event.set()
        await self._cancel_tasks(self._maintenance_loops)
        if self._jobs:
            for task in self._jobs.values():
                task.cancel()
            await asyncio.gather(*self._jobs.values(), return_exceptions=True)
        self._accepting_work = False
        self._draining = False
        self._stopped = True
        logger.info("Executor Worker stopped")

    @staticmethod
    async def _cancel_tasks(tasks: list[asyncio.Task[None]]) -> None:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        tasks.clear()

    async def _ensure_consumer_group(self) -> None:
        try:
            await self._redis.xgroup_create(
                self._settings.redis_work_stream,
                self._settings.execution_consumer_group,
                id="0",
                mkstream=True,
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def _stream_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                batches = await self._redis.xreadgroup(
                    groupname=self._settings.execution_consumer_group,
                    consumername=self._consumer_name,
                    streams={self._settings.redis_work_stream: ">"},
                    count=20,
                    block=1000,
                )
                for _stream, messages in batches:
                    for message_id, fields in messages:
                        await self._process_stream_message(message_id, fields)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Execution stream consumer failed")
                await asyncio.sleep(1)

    async def _pending_recovery_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._recover_pending_messages()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Execution pending-message recovery failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._settings.execution_pending_claim_interval_seconds,
                )
            except TimeoutError:
                pass

    async def _recover_pending_messages(self) -> int:
        result = await self._redis.xautoclaim(
            self._settings.redis_work_stream,
            self._settings.execution_consumer_group,
            self._consumer_name,
            min_idle_time=self._settings.execution_pending_claim_idle_milliseconds,
            start_id=self._pending_claim_cursor,
            count=self._settings.execution_pending_claim_batch_size,
        )
        next_cursor = result[0]
        messages = result[1]
        self._pending_claim_cursor = str(next_cursor)
        reclaimed = 0
        for message_id, fields in messages:
            reclaimed += 1
            await self._process_stream_message(message_id, fields)
        return reclaimed

    async def _process_stream_message(
        self,
        message_id: str,
        fields: dict[str, str],
    ) -> None:
        invalid_reason = _invalid_work_message_reason(fields)
        if invalid_reason is not None:
            try:
                await self._dead_letter(message_id, fields, invalid_reason)
                await self._ack_message(message_id)
            except Exception:
                logger.exception(
                    "Execution work message DLQ delivery failed",
                    extra={"message_id": message_id, "reason": invalid_reason},
                )
                return
            return
        try:
            await self._handle_work_message(fields)
        except Exception:
            logger.exception(
                "Execution work message handling failed",
                extra={"message_id": message_id},
            )
            return
        await self._ack_message(message_id)

    async def _ack_message(self, message_id: str) -> None:
        await self._redis.xack(
            self._settings.redis_work_stream,
            self._settings.execution_consumer_group,
            message_id,
        )

    async def _dead_letter(
        self,
        message_id: str,
        fields: dict[str, str],
        reason: str,
    ) -> None:
        context = extract_trace_context(fields)
        with self._tracing.span(
            "executor.redis.dead_letter",
            context=context,
            kind=SpanKind.PRODUCER,
            attributes={"executor.event.failure.reason": reason},
        ):
            await self._redis.xadd(
                self._settings.redis_work_dead_letter_stream,
                {
                    "source_stream": self._settings.redis_work_stream,
                    "source_message_id": message_id,
                    "message_id": _valid_uuid_or_empty(fields.get("message_id")),
                    "aggregate_id": _valid_uuid_or_empty(fields.get("aggregate_id")),
                    "reason": reason,
                    "dead_lettered_at": utc_now().isoformat(),
                },
            )

    async def _handle_work_message(self, fields: dict[str, str]) -> bool:
        message_type = fields.get("message_type")
        execution_id = UUID(fields["aggregate_id"])
        context = extract_trace_context(fields)
        with self._tracing.span(
            "executor.redis.consume",
            context=context,
            kind=SpanKind.CONSUMER,
            attributes={
                "executor.work.message_type": message_type,
                "executor.execution.id": str(execution_id),
            },
        ):
            if message_type in RUN_MESSAGE_TYPES:
                self._dispatch(execution_id, self._run_execution(execution_id))
            elif message_type == "execution.cancellation_ready":
                self._dispatch(
                    execution_id,
                    self._cancel_execution(execution_id),
                    replace=True,
                )
            else:
                return False
        return True

    async def _reconcile_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                async with self._session_factory() as session:
                    rows = list(
                        await session.execute(
                            select(
                                ExecutionORM.id,
                                ExecutionORM.status,
                                ExecutionORM.traceparent,
                                ExecutionORM.tracestate,
                            )
                            .where(
                                ExecutionORM.status.in_(
                                    [
                                        ExecutionStatus.QUEUED,
                                        ExecutionStatus.FINALIZING,
                                        ExecutionStatus.CANCEL_REQUESTED,
                                    ]
                                )
                            )
                            .order_by(ExecutionORM.created_at)
                            .limit(100)
                        )
                    )
                for execution_id, status, traceparent, tracestate in rows:
                    context = extract_trace_context(
                        {
                            "traceparent": traceparent or "",
                            "tracestate": tracestate or "",
                        }
                    )
                    with self._tracing.span(
                        "executor.reconcile",
                        context=context,
                        attributes={"executor.execution.id": str(execution_id)},
                    ):
                        if status == ExecutionStatus.CANCEL_REQUESTED:
                            self._dispatch(
                                execution_id,
                                self._cancel_execution(execution_id),
                                replace=True,
                            )
                        else:
                            self._dispatch(execution_id, self._run_execution(execution_id))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Execution reconciliation failed")
            await asyncio.sleep(2)

    async def _lease_recovery_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._recover_expired_leases()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Execution lease recovery failed")
            await asyncio.sleep(self._settings.execution_heartbeat_seconds)

    async def _retained_runtime_session_cleanup_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._cleanup_expired_retained_runtime_sessions()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Retained runtime session cleanup failed")
            await asyncio.sleep(self._settings.execution_heartbeat_seconds)

    async def _multi_lifecycle_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._audit_multi_lifecycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Dynamic execution lifecycle audit failed")
            await asyncio.sleep(self._settings.execution_heartbeat_seconds)

    def _dispatch(
        self,
        execution_id: UUID,
        coroutine: Coroutine[Any, Any, None],
        *,
        replace: bool = False,
    ) -> None:
        current = self._jobs.get(execution_id)
        if not self._accepting_work and not replace:
            coroutine.close()
            return
        if current is not None and not current.done():
            if replace:
                current.cancel()
                task = asyncio.create_task(coroutine, name=f"cancel-{execution_id}")
                self._jobs[execution_id] = task
                self._jobs_idle.clear()
                task.add_done_callback(lambda done: self._remove_job_if_current(execution_id, done))
            else:
                coroutine.close()
            return
        task = asyncio.create_task(coroutine, name=f"execution-{execution_id}")
        self._jobs[execution_id] = task
        self._jobs_idle.clear()
        task.add_done_callback(lambda done: self._remove_job_if_current(execution_id, done))

    def _remove_job_if_current(self, execution_id: UUID, task: asyncio.Task[None]) -> None:
        if self._jobs.get(execution_id) is task:
            self._jobs.pop(execution_id, None)
            if not self._jobs:
                self._jobs_idle.set()

    async def _run_execution(self, execution_id: UUID) -> None:
        pool = await self._execution_pool(execution_id)
        if pool is None:
            return
        with self._tracing.span(
            "executor.worker.execution",
            kind=SpanKind.CONSUMER,
            attributes={
                "executor.execution.id": str(execution_id),
                "executor.runtime.pool": pool.value,
            },
        ):
            await self._run_execution_impl(execution_id, pool)

    async def _execution_pool(self, execution_id: UUID) -> RuntimePool | None:
        async with self._session_factory() as session:
            return await session.scalar(
                select(ExecutionORM.runtime_pool).where(ExecutionORM.id == execution_id)
            )

    @asynccontextmanager
    async def _pool_activity(self, pool: RuntimePool) -> AsyncIterator[None]:
        del pool
        yield

    async def _trace_runtime[T](
        self,
        name: str,
        operation: Awaitable[T],
        *,
        execution_id: UUID,
        target_id: UUID,
        sequence: int | None = None,
    ) -> T:
        attributes: dict[str, object] = {
            "executor.execution.id": str(execution_id),
            "executor.runtime.target.id": str(target_id),
        }
        if sequence is not None:
            attributes["executor.step.sequence"] = sequence
        with self._tracing.span(name, attributes=attributes):
            return await operation

    async def _run_execution_impl(self, execution_id: UUID, pool: RuntimePool) -> None:
        async with self._pool_activity(pool):
            claimed = await self._claim(execution_id)
            if claimed is None:
                return
            execution, target, attempt_id = claimed
            if execution.operation_mode == OperationMode.MULTI:
                await self._run_multi_execution(execution, target, attempt_id)
                return
            driver = self._create_driver(target)
            runtime_session_id: str | None = None
            heartbeat: asyncio.Task[None] | None = None
            failed_sequence: int | None = None
            try:
                resume = (
                    execution.retry_count > 0
                    and execution.retry_strategy == RetryStrategy.FROM_FAILED_STEP
                    and execution.retry_from_sequence is not None
                    and execution.runtime_session_id is not None
                )
                if resume:
                    runtime_session_id = execution.runtime_session_id
                    try:
                        session_exists = await self._trace_runtime(
                            "executor.runtime.session.exists",
                            driver.session_exists(runtime_session_id),
                            execution_id=execution.id,
                            target_id=target.id,
                        )
                    except RuntimeDriverError as exc:
                        await self._defer_retained_retry(
                            execution.id,
                            attempt_id,
                            target.id,
                            f"{type(exc).__name__}: retained runtime session preflight failed",
                        )
                        return
                    if not session_exists:
                        raise RetainedRuntimeSessionLostError(
                            "The retained Runtime session no longer exists."
                        )
                workspace = self._workspace.plan(execution)
                await self._trace_runtime(
                    "executor.runtime.workspace.prepare",
                    driver.prepare_workspace(workspace.runtime_relative_path),
                    execution_id=execution.id,
                    target_id=target.id,
                )
                cells = self._workspace.load_cells(execution)
                await self._ensure_steps(execution.id, len(cells))
                start_sequence = execution.retry_from_sequence if resume else 0
                if not resume:
                    runtime_session_id = await self._trace_runtime(
                        "executor.runtime.session.start",
                        driver.start_session(
                            execution.runtime_profile, workspace.runtime_relative_path
                        ),
                        execution_id=execution.id,
                        target_id=target.id,
                    )
                if runtime_session_id is None:
                    raise RuntimeError("Runtime session ID was not established.")
                await self._record_runtime_session(
                    execution.id,
                    attempt_id,
                    runtime_session_id,
                    workspace.runtime_relative_path,
                    workspace.notebook_path,
                )
                heartbeat = asyncio.create_task(
                    self._heartbeat(execution.id, attempt_id),
                    name=f"heartbeat-{execution.id}",
                )
                all_outputs: list[list[dict[str, object]]] = [
                    step.outputs for step in execution.steps if step.sequence < start_sequence
                ]
                execution_counts: list[int | None] = [None] * len(all_outputs)
                for sequence in range(start_sequence, len(cells)):
                    code = cells[sequence]
                    artifact_snapshot = await self._artifacts.snapshot(driver, workspace)
                    await self._step_started(execution.id, attempt_id, sequence)
                    try:
                        result = await self._trace_runtime(
                            "executor.runtime.code.execute",
                            self._execute_runtime_step(
                                driver,
                                runtime_session_id,
                                code,
                                execution.id,
                                sequence,
                            ),
                            execution_id=execution.id,
                            target_id=target.id,
                            sequence=sequence,
                        )
                    except asyncio.CancelledError:
                        # A user cancellation or Worker drain can interrupt a cell after it has
                        # written files. Preserve those files as incomplete execution evidence
                        # before the outer cancellation path tears down the Runtime session.
                        try:
                            await self._artifacts.discover_and_register(
                                driver=driver,
                                workspace=workspace,
                                before=artifact_snapshot,
                                execution_id=execution.id,
                                attempt_id=attempt_id,
                                sequence=sequence,
                                status=ArtifactStatus.INCOMPLETE,
                            )
                        except Exception as artifact_exc:
                            await self._record_artifact_failure(
                                execution.id,
                                attempt_id,
                                sequence,
                                artifact_exc,
                            )
                            logger.warning(
                                "Cancelled-cell Artifact registration failed",
                                extra={"execution_id": str(execution.id)},
                            )
                        raise
                    except RuntimeExecutionError as exc:
                        failed_sequence = sequence
                        await self._step_failed(
                            execution.id,
                            attempt_id,
                            sequence,
                            exc.outputs,
                            str(exc),
                        )
                        all_outputs.append(exc.outputs)
                        execution_counts.append(None)
                        await driver.write_notebook(
                            workspace.notebook_path,
                            self._workspace.notebook_document(
                                workspace,
                                execution.runtime_profile,
                                cells[: sequence + 1],
                                all_outputs,
                                execution_counts,
                            ),
                        )
                        try:
                            await self._artifacts.discover_and_register(
                                driver=driver,
                                workspace=workspace,
                                before=artifact_snapshot,
                                execution_id=execution.id,
                                attempt_id=attempt_id,
                                sequence=sequence,
                                status=ArtifactStatus.INCOMPLETE,
                            )
                        except Exception as artifact_exc:
                            await self._record_artifact_failure(
                                execution.id,
                                attempt_id,
                                sequence,
                                artifact_exc,
                            )
                            logger.warning(
                                "Incomplete Artifact registration failed",
                                extra={"execution_id": str(execution.id)},
                            )
                        raise
                    all_outputs.append(result.outputs)
                    execution_counts.append(result.execution_count)
                    await self._step_succeeded(
                        execution.id,
                        attempt_id,
                        sequence,
                        result.outputs,
                        result.execution_count,
                    )
                    await driver.write_notebook(
                        workspace.notebook_path,
                        self._workspace.notebook_document(
                            workspace,
                            execution.runtime_profile,
                            cells[: sequence + 1],
                            all_outputs,
                            execution_counts,
                        ),
                    )
                    try:
                        await self._artifacts.discover_and_register(
                            driver=driver,
                            workspace=workspace,
                            before=artifact_snapshot,
                            execution_id=execution.id,
                            attempt_id=attempt_id,
                            sequence=sequence,
                            status=ArtifactStatus.AVAILABLE,
                        )
                    except Exception as artifact_exc:
                        await self._record_artifact_failure(
                            execution.id,
                            attempt_id,
                            sequence,
                            artifact_exc,
                        )
                        raise
                await self._artifacts.register_notebook(
                    driver=driver,
                    workspace=workspace,
                    execution_id=execution.id,
                    attempt_id=attempt_id,
                    sequence=len(cells) - 1,
                )
                await self._trace_runtime(
                    "executor.runtime.session.delete",
                    driver.delete_session(runtime_session_id),
                    execution_id=execution.id,
                    target_id=target.id,
                )
                await self._finalize(
                    execution.id,
                    attempt_id,
                    ExecutionStatus.SUCCEEDED,
                    runtime_session_cleanup_status=RuntimeSessionCleanupStatus.SUCCEEDED,
                )
            except asyncio.CancelledError:
                if await self._cancellation_job_owns_terminal(execution.id):
                    # The replacement cancellation job exclusively owns Runtime cleanup and the
                    # CANCELLED transition. This execution job only preserves cell evidence.
                    raise
                cleanup_status = RuntimeSessionCleanupStatus.NOT_REQUIRED
                if runtime_session_id is not None:
                    try:
                        async with asyncio.timeout(
                            self._settings.execution_shutdown_cleanup_seconds
                        ):
                            cleanup_status = await _best_effort_session_stop(
                                driver, runtime_session_id
                            )
                    except TimeoutError:
                        cleanup_status = RuntimeSessionCleanupStatus.FAILED
                        logger.warning(
                            "Runtime session cleanup exceeded the Worker shutdown deadline",
                            extra={"execution_id": str(execution.id)},
                        )
                await self._finalize(
                    execution.id,
                    attempt_id,
                    ExecutionStatus.FAILED,
                    "Executor worker stopped while the execution was running.",
                    failure_type=FailureType.WORKER_SHUTDOWN,
                    retry_strategy=RetryStrategy.FROM_START,
                    runtime_session_cleanup_status=cleanup_status,
                )
                raise
            except Exception as exc:
                retain_session = (
                    isinstance(exc, RuntimeExecutionError)
                    and runtime_session_id is not None
                    and failed_sequence is not None
                )
                failure_type, retry_strategy = _failure_policy(exc, retain_session)
                cleanup_status = RuntimeSessionCleanupStatus.NOT_REQUIRED
                if runtime_session_id is not None and not retain_session:
                    cleanup_status = await _best_effort_session_stop(driver, runtime_session_id)
                await self._finalize(
                    execution.id,
                    attempt_id,
                    ExecutionStatus.FAILED,
                    _safe_error(exc),
                    retain_session=retain_session,
                    retry_from_sequence=failed_sequence,
                    failure_type=failure_type,
                    retry_strategy=retry_strategy,
                    runtime_session_cleanup_status=cleanup_status,
                )
            finally:
                if heartbeat is not None:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
                await driver.close()

    async def _claim(self, execution_id: UUID) -> tuple[Any, RuntimeTargetORM, UUID] | None:
        now = utc_now()
        lease_expires = now + timedelta(seconds=self._settings.execution_lease_seconds)
        async with self._session_factory() as session, session.begin():
            execution_row = await session.scalar(
                select(ExecutionORM)
                .where(ExecutionORM.id == execution_id)
                .options(selectinload(ExecutionORM.steps))
                .with_for_update()
            )
            if execution_row is None or execution_row.status not in {
                ExecutionStatus.QUEUED,
                ExecutionStatus.FINALIZING,
            }:
                return None
            operation: ExecutionOperationORM | None = None
            if (
                not execution_row.finalization_requested
                and execution_row.active_operation_id is not None
            ):
                operation = await session.scalar(
                    select(ExecutionOperationORM)
                    .where(
                        ExecutionOperationORM.id == execution_row.active_operation_id,
                        ExecutionOperationORM.execution_id == execution_id,
                        ExecutionOperationORM.status == OperationStatus.QUEUED,
                    )
                    .with_for_update()
                )
                if operation is None:
                    return None
            elif not execution_row.finalization_requested and execution_row.retry_count == 0:
                return None
            if (
                execution_row.operation_mode == OperationMode.MULTI
                and execution_row.runtime_session_id is not None
                and execution_row.runtime_target_id is not None
            ):
                waiting_attempt = await session.scalar(
                    select(ExecutionAttemptORM)
                    .where(
                        ExecutionAttemptORM.execution_id == execution_id,
                        ExecutionAttemptORM.status == AttemptStatus.WAITING,
                    )
                    .with_for_update()
                )
                target = await session.scalar(
                    select(RuntimeTargetORM)
                    .where(RuntimeTargetORM.id == execution_row.runtime_target_id)
                    .with_for_update()
                )
                if (
                    waiting_attempt is None
                    or target is None
                    or not target.enabled
                    or target.status == RuntimeTargetStatus.OFFLINE
                    or target.runtime_type != execution_row.runtime_type
                    or target.pool != execution_row.runtime_pool
                    or waiting_attempt.runtime_type != execution_row.runtime_type
                    or waiting_attempt.runtime_profile != execution_row.runtime_profile
                    or execution_row.runtime_profile not in target.supported_profiles
                ):
                    return None
                waiting_attempt.status = AttemptStatus.RUNNING
                waiting_attempt.lease_owner = self._consumer_name
                waiting_attempt.lease_expires_at = lease_expires
                waiting_attempt.heartbeat_at = now
                execution_row.status = ExecutionStatus.RUNNING
                execution_row.lease_owner = self._consumer_name
                execution_row.lease_expires_at = lease_expires
                execution_row.heartbeat_at = now
                execution_row.execution_expires_at = execution_row.execution_expires_at or (
                    execution_row.started_at or now
                ) + timedelta(seconds=self._settings.execution_max_runtime_seconds)
                execution_row.updated_at = now
                execution_row.version += 1
                if operation is not None:
                    operation.status = OperationStatus.RUNNING
                    operation.execution_attempt_id = waiting_attempt.id
                    operation.started_at = now
                    operation.updated_at = now
                await _add_outbox(
                    session,
                    execution_id,
                    "execution.resumed",
                    ExecutionStatus.RUNNING,
                )
                return execution_row.to_domain(), target, waiting_attempt.id
            is_resume = (
                execution_row.retry_count > 0
                and execution_row.retry_strategy == RetryStrategy.FROM_FAILED_STEP
                and execution_row.retry_from_sequence is not None
                and execution_row.runtime_session_id is not None
                and execution_row.runtime_target_id is not None
            )
            if is_resume:
                if (
                    execution_row.retained_runtime_session_until is None
                    or _as_utc(execution_row.retained_runtime_session_until) <= now
                ):
                    # The retained-session cleanup loop owns expiry finalization and cleanup.
                    return None
                target = await session.scalar(
                    select(RuntimeTargetORM)
                    .where(RuntimeTargetORM.id == execution_row.runtime_target_id)
                    .with_for_update()
                )
                if (
                    target is None
                    or not target.enabled
                    or target.runtime_type != execution_row.runtime_type
                    or target.pool != execution_row.runtime_pool
                    or execution_row.runtime_profile not in target.supported_profiles
                ):
                    await self._fail_unavailable_retained_retry(
                        session,
                        execution_row,
                        now,
                        "The retained Runtime Target is missing or disabled before retry.",
                    )
                    return None
                if target.status == RuntimeTargetStatus.OFFLINE:
                    # OFFLINE can be temporary. Keep the retry pinned to the original target and
                    # session until health monitoring recovers it or the retention window expires.
                    return None
            if not is_resume:
                target = await self._select_target(session, execution_row)
            if target is None:
                return None
            attempt_number = (
                await session.scalar(
                    select(func.count(ExecutionAttemptORM.id)).where(
                        ExecutionAttemptORM.execution_id == execution_id
                    )
                )
                or 0
            ) + 1
            attempt_id = uuid4()
            session.add(
                ExecutionAttemptORM(
                    id=attempt_id,
                    execution_id=execution_id,
                    attempt_number=attempt_number,
                    runtime_type=execution_row.runtime_type,
                    runtime_profile=execution_row.runtime_profile,
                    runtime_target_id=target.id,
                    status=AttemptStatus.RUNNING,
                    lease_owner=self._consumer_name,
                    lease_expires_at=lease_expires,
                    heartbeat_at=now,
                    created_by_type=(
                        execution_row.updated_by_type or execution_row.created_by_type
                    ),
                    created_by=execution_row.updated_by or execution_row.created_by,
                    updated_by_type=(
                        execution_row.updated_by_type or execution_row.created_by_type
                    ),
                    updated_by=execution_row.updated_by or execution_row.created_by,
                    started_at=now,
                )
            )
            if operation is not None:
                operation.status = OperationStatus.RUNNING
                operation.execution_attempt_id = attempt_id
                operation.started_at = now
                operation.updated_at = now
            execution_row.status = ExecutionStatus.RUNNING
            execution_row.runtime_target_id = target.id
            execution_row.lease_owner = self._consumer_name
            execution_row.lease_expires_at = lease_expires
            execution_row.heartbeat_at = now
            started_at = execution_row.started_at or now
            execution_row.started_at = started_at
            execution_row.execution_expires_at = (
                execution_row.execution_expires_at
                or started_at + timedelta(seconds=self._settings.execution_max_runtime_seconds)
            )
            execution_row.error_message = None
            execution_row.failure_type = None
            if not is_resume:
                execution_row.retained_runtime_session_until = None
            execution_row.runtime_session_cleanup_status = RuntimeSessionCleanupStatus.NOT_REQUIRED
            execution_row.updated_at = now
            execution_row.version += 1
            await _add_outbox(session, execution_id, "execution.started", ExecutionStatus.RUNNING)
            return execution_row.to_domain(), target, attempt_id

    async def _defer_retained_retry(
        self,
        execution_id: UUID,
        attempt_id: UUID,
        target_id: UUID,
        diagnostic: str,
    ) -> None:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            execution = await session.scalar(
                select(ExecutionORM).where(ExecutionORM.id == execution_id).with_for_update()
            )
            attempt = await session.scalar(
                select(ExecutionAttemptORM)
                .where(ExecutionAttemptORM.id == attempt_id)
                .with_for_update()
            )
            target = await session.scalar(
                select(RuntimeTargetORM).where(RuntimeTargetORM.id == target_id).with_for_update()
            )
            if (
                execution is None
                or attempt is None
                or execution.status != ExecutionStatus.RUNNING
                or attempt.status != AttemptStatus.RUNNING
                or execution.retry_strategy != RetryStrategy.FROM_FAILED_STEP
            ):
                return
            execution.status = ExecutionStatus.QUEUED
            execution.error_message = (
                "The retained Runtime Target is temporarily unavailable; waiting for recovery."
            )
            execution.failure_type = FailureType.TOOL_ERROR
            execution.lease_owner = None
            execution.lease_expires_at = None
            execution.heartbeat_at = None
            execution.updated_at = now
            execution.version += 1
            attempt.status = AttemptStatus.FAILED
            attempt.lease_owner = None
            attempt.lease_expires_at = None
            attempt.error_message = execution.error_message
            attempt.failure_type = FailureType.RUNTIME_UNAVAILABLE
            attempt.retry_strategy = RetryStrategy.FROM_FAILED_STEP
            attempt.runtime_session_cleanup_status = RuntimeSessionCleanupStatus.NOT_REQUIRED
            attempt.finished_at = now
            if execution.active_operation_id is not None:
                await session.execute(
                    update(ExecutionOperationORM)
                    .where(
                        ExecutionOperationORM.id == execution.active_operation_id,
                        ExecutionOperationORM.status == OperationStatus.RUNNING,
                    )
                    .values(
                        status=OperationStatus.QUEUED,
                        execution_attempt_id=None,
                        error_message=None,
                        started_at=None,
                        finished_at=None,
                        updated_at=now,
                    )
                )
            if target is not None:
                target.status = RuntimeTargetStatus.OFFLINE
                target.last_health_check_at = now
                target.last_health_error = diagnostic[:500]
                target.updated_at = now
            await _add_outbox(
                session,
                execution.id,
                "execution.retry_deferred",
                ExecutionStatus.QUEUED,
                {
                    "failure_type": FailureType.RUNTIME_UNAVAILABLE.value,
                    "retry_strategy": RetryStrategy.FROM_FAILED_STEP.value,
                    "reason": "retained_target_temporarily_unavailable",
                    "runtime_target_id": str(target_id),
                },
            )

    async def _fail_unavailable_retained_retry(
        self,
        session: AsyncSession,
        execution: ExecutionORM,
        now: datetime,
        error_message: str,
    ) -> None:
        execution.status = ExecutionStatus.FAILED
        execution.error_message = error_message
        execution.failure_type = FailureType.RUNTIME_UNAVAILABLE
        execution.finished_at = now
        execution.updated_at = now
        execution.lease_owner = None
        execution.lease_expires_at = None
        execution.heartbeat_at = None
        execution.retry_strategy = RetryStrategy.FROM_START
        execution.retry_from_sequence = 0
        execution.retained_runtime_session_until = None
        execution.runtime_session_cleanup_status = RuntimeSessionCleanupStatus.FAILED
        execution.version += 1
        await session.execute(
            update(ExecutionStepORM)
            .where(
                ExecutionStepORM.execution_id == execution.id,
                ExecutionStepORM.status == StepStatus.PENDING,
            )
            .values(status=StepStatus.SKIPPED, finished_at=now, updated_at=now)
        )
        await self._fail_active_operation_without_attempt(
            session,
            execution,
            now,
            error_message,
        )
        await _add_outbox(
            session,
            execution.id,
            "execution.failed",
            ExecutionStatus.FAILED,
            {
                "failure_type": FailureType.RUNTIME_UNAVAILABLE.value,
                "retry_strategy": RetryStrategy.FROM_START.value,
                "retry_from_sequence": 0,
                "runtime_session_cleanup_status": RuntimeSessionCleanupStatus.FAILED.value,
                "reason": "retained_target_unavailable",
            },
        )

    async def _select_target(
        self, session: AsyncSession, execution: ExecutionORM
    ) -> RuntimeTargetORM | None:
        targets = list(
            await session.scalars(
                select(RuntimeTargetORM)
                .where(
                    RuntimeTargetORM.pool == execution.runtime_pool,
                    RuntimeTargetORM.runtime_type == execution.runtime_type,
                    RuntimeTargetORM.enabled.is_(True),
                    RuntimeTargetORM.status == RuntimeTargetStatus.ACTIVE,
                )
                .order_by(RuntimeTargetORM.name)
                .with_for_update(skip_locked=True)
            )
        )
        candidates: list[tuple[RuntimeTargetORM, int]] = []
        now = utc_now()
        for target in targets:
            if execution.runtime_profile not in target.supported_profiles:
                continue
            running = await session.scalar(
                select(func.count(ExecutionAttemptORM.id)).where(
                    ExecutionAttemptORM.runtime_target_id == target.id,
                    ExecutionAttemptORM.status.in_([AttemptStatus.RUNNING, AttemptStatus.WAITING]),
                )
            )
            retained = await session.scalar(
                select(func.count(ExecutionORM.id)).where(
                    ExecutionORM.runtime_target_id == target.id,
                    ExecutionORM.status.in_([ExecutionStatus.FAILED, ExecutionStatus.QUEUED]),
                    ExecutionORM.retry_strategy == RetryStrategy.FROM_FAILED_STEP,
                    ExecutionORM.retained_runtime_session_until > now,
                )
            )
            reserved = (running or 0) + (retained or 0)
            if reserved < target.max_concurrent_executions:
                candidates.append((target, reserved))
        if not candidates:
            return None

        fresh_candidates = [
            candidate
            for candidate in candidates
            if self._has_fresh_resource_observation(candidate[0], now)
        ]
        if fresh_candidates:
            admitted = [
                candidate
                for candidate in fresh_candidates
                if candidate[0].memory_utilization is None
                or candidate[0].memory_utilization < self._settings.runtime_memory_admission_limit
            ]
            if not admitted:
                return None
            return min(admitted, key=self._resource_candidate_key)[0]

        return min(
            candidates,
            key=lambda candidate: (
                candidate[1] / candidate[0].max_concurrent_executions,
                candidate[1],
                candidate[0].name,
            ),
        )[0]

    def _has_fresh_resource_observation(self, target: RuntimeTargetORM, now: datetime) -> bool:
        observed_at = target.resource_observed_at
        if observed_at is None or target.resource_last_error is not None:
            return False
        if target.cpu_utilization is None and target.memory_utilization is None:
            return False
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=UTC)
        return observed_at >= now - timedelta(
            seconds=self._settings.runtime_resource_max_age_seconds
        )

    @staticmethod
    def _resource_candidate_key(
        candidate: tuple[RuntimeTargetORM, int],
    ) -> tuple[float, float, int, str]:
        target, reserved = candidate
        pressure = max(
            reserved / target.max_concurrent_executions,
            *(
                value
                for value in (target.cpu_utilization, target.memory_utilization)
                if value is not None
            ),
        )
        memory = (
            target.memory_utilization if target.memory_utilization is not None else float("inf")
        )
        return pressure, memory, reserved, target.name

    async def _run_multi_execution(
        self, execution: Execution, target: RuntimeTargetORM, attempt_id: UUID
    ) -> None:
        driver = self._create_driver(target)
        heartbeat: asyncio.Task[None] | None = None
        runtime_session_id = execution.runtime_session_id
        try:
            workspace = self._workspace.plan(execution)
            await self._trace_runtime(
                "executor.runtime.workspace.prepare",
                driver.prepare_workspace(workspace.runtime_relative_path),
                execution_id=execution.id,
                target_id=target.id,
            )
            if runtime_session_id is None:
                runtime_session_id = await self._trace_runtime(
                    "executor.runtime.session.start",
                    driver.start_session(
                        execution.runtime_profile, workspace.runtime_relative_path
                    ),
                    execution_id=execution.id,
                    target_id=target.id,
                )
            await self._record_runtime_session(
                execution.id,
                attempt_id,
                runtime_session_id,
                workspace.runtime_relative_path,
                workspace.notebook_path,
            )
            heartbeat = asyncio.create_task(
                self._heartbeat(execution.id, attempt_id),
                name=f"heartbeat-{execution.id}",
            )
            if execution.finalization_requested:
                last_sequence = max((step.sequence for step in execution.steps), default=0)
                await self._artifacts.register_notebook(
                    driver=driver,
                    workspace=workspace,
                    execution_id=execution.id,
                    attempt_id=attempt_id,
                    sequence=last_sequence,
                )
                await self._trace_runtime(
                    "executor.runtime.session.delete",
                    driver.delete_session(runtime_session_id),
                    execution_id=execution.id,
                    target_id=target.id,
                )
                await self._finalize(
                    execution.id,
                    attempt_id,
                    ExecutionStatus.SUCCEEDED,
                    runtime_session_cleanup_status=RuntimeSessionCleanupStatus.SUCCEEDED,
                )
                return

            operation_id = execution.active_operation_id
            if operation_id is None:
                raise ValueError("Queued MULTI execution has no active Operation.")
            pending_steps = [
                step
                for step in execution.steps
                if step.operation_id == operation_id and step.status == StepStatus.PENDING
            ]
            if not pending_steps:
                raise ValueError("Queued MULTI Operation has no pending Step.")
            for pending in pending_steps:
                if not pending.code:
                    raise ValueError("Queued MULTI Operation contains a blank Step payload.")
                artifact_snapshot = await self._artifacts.snapshot(driver, workspace)
                await self._step_started(execution.id, attempt_id, pending.sequence)
                try:
                    result = await self._trace_runtime(
                        "executor.runtime.code.execute",
                        self._execute_runtime_step(
                            driver,
                            runtime_session_id,
                            pending.code,
                            execution.id,
                            pending.sequence,
                        ),
                        execution_id=execution.id,
                        target_id=target.id,
                        sequence=pending.sequence,
                    )
                except asyncio.CancelledError:
                    try:
                        await self._artifacts.discover_and_register(
                            driver=driver,
                            workspace=workspace,
                            before=artifact_snapshot,
                            execution_id=execution.id,
                            attempt_id=attempt_id,
                            sequence=pending.sequence,
                            status=ArtifactStatus.INCOMPLETE,
                        )
                    except Exception as artifact_exc:
                        await self._record_artifact_failure(
                            execution.id, attempt_id, pending.sequence, artifact_exc
                        )
                    raise
                except RuntimeExecutionError as exc:
                    await self._step_failed(
                        execution.id, attempt_id, pending.sequence, exc.outputs, str(exc)
                    )
                    await self._skip_operation_steps_after(
                        execution.id, operation_id, pending.sequence
                    )
                    await self._write_multi_notebook(
                        driver, execution.id, execution.runtime_profile, workspace
                    )
                    try:
                        await self._artifacts.discover_and_register(
                            driver=driver,
                            workspace=workspace,
                            before=artifact_snapshot,
                            execution_id=execution.id,
                            attempt_id=attempt_id,
                            sequence=pending.sequence,
                            status=ArtifactStatus.INCOMPLETE,
                        )
                    except Exception as artifact_exc:
                        await self._record_artifact_failure(
                            execution.id, attempt_id, pending.sequence, artifact_exc
                        )
                    await self._complete_multi_operation(
                        execution.id,
                        attempt_id,
                        operation_id,
                        OperationStatus.FAILED,
                        failed_sequence=pending.sequence,
                        error_message=str(exc),
                    )
                    return
                await self._step_succeeded(
                    execution.id,
                    attempt_id,
                    pending.sequence,
                    result.outputs,
                    result.execution_count,
                )
                await self._write_multi_notebook(
                    driver, execution.id, execution.runtime_profile, workspace
                )
                try:
                    await self._artifacts.discover_and_register(
                        driver=driver,
                        workspace=workspace,
                        before=artifact_snapshot,
                        execution_id=execution.id,
                        attempt_id=attempt_id,
                        sequence=pending.sequence,
                        status=ArtifactStatus.AVAILABLE,
                    )
                except Exception as artifact_exc:
                    await self._record_artifact_failure(
                        execution.id, attempt_id, pending.sequence, artifact_exc
                    )
                    raise
            await self._complete_multi_operation(
                execution.id,
                attempt_id,
                operation_id,
                OperationStatus.SUCCEEDED,
            )
        except asyncio.CancelledError:
            if await self._cancellation_job_owns_terminal(execution.id):
                # Avoid racing the replacement cancellation job for session deletion and the
                # terminal event. The interrupted-cell handler above already preserved evidence.
                raise
            cleanup_status = RuntimeSessionCleanupStatus.NOT_REQUIRED
            if runtime_session_id is not None:
                cleanup_status = await _best_effort_session_stop(driver, runtime_session_id)
            await self._finalize(
                execution.id,
                attempt_id,
                ExecutionStatus.FAILED,
                "Executor worker stopped while a MULTI Operation Step was running.",
                failure_type=FailureType.WORKER_SHUTDOWN,
                retry_strategy=RetryStrategy.NOT_RETRYABLE,
                runtime_session_cleanup_status=cleanup_status,
            )
            raise
        except Exception as exc:
            cleanup_status = RuntimeSessionCleanupStatus.NOT_REQUIRED
            if runtime_session_id is not None:
                cleanup_status = await _best_effort_session_stop(driver, runtime_session_id)
            await self._finalize(
                execution.id,
                attempt_id,
                ExecutionStatus.FAILED,
                _safe_error(exc),
                failure_type=_failure_policy(exc, False)[0],
                retry_strategy=RetryStrategy.NOT_RETRYABLE,
                runtime_session_cleanup_status=cleanup_status,
            )
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            await driver.close()

    async def _cancellation_job_owns_terminal(self, execution_id: UUID) -> bool:
        async with self._session_factory() as session:
            status = await session.scalar(
                select(ExecutionORM.status).where(ExecutionORM.id == execution_id)
            )
        return status in {ExecutionStatus.CANCEL_REQUESTED, ExecutionStatus.CANCELLED}

    async def _write_multi_notebook(
        self,
        driver: RuntimeDriver,
        execution_id: UUID,
        runtime_profile: str,
        workspace: ExecutionWorkspace,
    ) -> None:
        async with self._session_factory() as session:
            steps = list(
                await session.scalars(
                    select(ExecutionStepORM)
                    .where(ExecutionStepORM.execution_id == execution_id)
                    .order_by(ExecutionStepORM.sequence)
                )
            )
        executed_steps = [
            step for step in steps if step.status in {StepStatus.SUCCEEDED, StepStatus.FAILED}
        ]
        cells = [step.code or "" for step in executed_steps]
        outputs = [step.outputs for step in executed_steps]
        # MULTI Steps execute exactly once, sequentially, on one retained session. SKIPPED
        # planned Steps never become notebook cells, so kernel history is the executed-cell order.
        execution_counts: list[int | None] = list(range(1, len(executed_steps) + 1))
        await driver.write_notebook(
            workspace.notebook_path,
            self._workspace.notebook_document(
                workspace, runtime_profile, cells, outputs, execution_counts
            ),
        )

    async def _complete_multi_operation(
        self,
        execution_id: UUID,
        attempt_id: UUID,
        operation_id: UUID,
        operation_status: OperationStatus,
        *,
        failed_sequence: int | None = None,
        error_message: str | None = None,
    ) -> None:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            execution = await session.scalar(
                select(ExecutionORM).where(ExecutionORM.id == execution_id).with_for_update()
            )
            if execution is None or execution.status != ExecutionStatus.RUNNING:
                return
            operation = await session.scalar(
                select(ExecutionOperationORM)
                .where(
                    ExecutionOperationORM.id == operation_id,
                    ExecutionOperationORM.execution_id == execution_id,
                )
                .with_for_update()
            )
            if operation is None or operation.status != OperationStatus.RUNNING:
                return
            execution.status = ExecutionStatus.WAITING_FOR_OPERATION
            execution.lease_owner = None
            execution.lease_expires_at = None
            execution.updated_at = now
            execution.finalization_requested = False
            if execution.operation_wait_timeout_seconds is None:
                raise ValueError("MULTI execution has no Operation wait timeout.")
            wait_deadline = now + timedelta(seconds=execution.operation_wait_timeout_seconds)
            execution.operation_wait_expires_at = min(
                wait_deadline,
                (
                    _as_utc(execution.execution_expires_at)
                    if execution.execution_expires_at is not None
                    else wait_deadline
                ),
            )
            execution.version += 1
            operation.status = operation_status
            operation.error_message = error_message[:2000] if error_message else None
            operation.finished_at = now
            operation.updated_at = now
            await session.execute(
                update(ExecutionAttemptORM)
                .where(ExecutionAttemptORM.id == attempt_id)
                .values(
                    status=AttemptStatus.WAITING,
                    lease_owner=None,
                    lease_expires_at=None,
                )
            )
            await _add_outbox(
                session,
                execution_id,
                f"execution.operation_{operation_status.value.lower()}",
                ExecutionStatus.WAITING_FOR_OPERATION,
                {
                    "execution_attempt_id": str(attempt_id),
                    "operation_id": str(operation_id),
                    "operation_status": operation_status.value,
                    "first_sequence": operation.first_sequence,
                    "last_sequence": operation.last_sequence,
                    **({"failed_sequence": failed_sequence} if failed_sequence is not None else {}),
                    "version": execution.version,
                    **(
                        {"error_message": error_message or "Operation failed."}
                        if operation_status == OperationStatus.FAILED
                        else {}
                    ),
                },
            )
            await _add_outbox(
                session,
                execution_id,
                "execution.waiting_for_operation",
                ExecutionStatus.WAITING_FOR_OPERATION,
                {
                    "operation_id": str(operation_id),
                    "operation_wait_expires_at": execution.operation_wait_expires_at,
                    "version": execution.version,
                },
            )

    async def _skip_operation_steps_after(
        self, execution_id: UUID, operation_id: UUID, failed_sequence: int
    ) -> None:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(ExecutionStepORM)
                .where(
                    ExecutionStepORM.execution_id == execution_id,
                    ExecutionStepORM.operation_id == operation_id,
                    ExecutionStepORM.sequence > failed_sequence,
                    ExecutionStepORM.status == StepStatus.PENDING,
                )
                .values(status=StepStatus.SKIPPED, finished_at=now, updated_at=now)
            )

    async def _ensure_steps(self, execution_id: UUID, cell_count: int) -> None:
        async with self._session_factory() as session, session.begin():
            steps = list(
                await session.scalars(
                    select(ExecutionStepORM)
                    .where(ExecutionStepORM.execution_id == execution_id)
                    .order_by(ExecutionStepORM.sequence)
                )
            )
            if steps and len(steps) != cell_count:
                raise ValueError(
                    f"Execution has {len(steps)} planned steps but source has {cell_count} cells."
                )
            if not steps:
                session.add_all(
                    [
                        ExecutionStepORM(
                            execution_id=execution_id,
                            sequence=index,
                            status=StepStatus.PENDING,
                            input_parameters={},
                            outputs=[],
                        )
                        for index in range(cell_count)
                    ]
                )

    async def _record_runtime_session(
        self,
        execution_id: UUID,
        attempt_id: UUID,
        runtime_session_id: str,
        workspace_path: str,
        notebook_path: str,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(ExecutionORM)
                .where(ExecutionORM.id == execution_id)
                .values(
                    runtime_session_id=runtime_session_id,
                    workspace_path=workspace_path,
                    notebook_path=notebook_path,
                    updated_at=utc_now(),
                )
            )
            await session.execute(
                update(ExecutionAttemptORM)
                .where(ExecutionAttemptORM.id == attempt_id)
                .values(runtime_session_id=runtime_session_id)
            )

    async def _step_started(self, execution_id: UUID, attempt_id: UUID, sequence: int) -> None:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            step = await session.scalar(
                select(ExecutionStepORM).where(
                    ExecutionStepORM.execution_id == execution_id,
                    ExecutionStepORM.sequence == sequence,
                )
            )
            if step is None:
                raise ValueError(f"Execution Step {sequence} was not found.")
            step.status = StepStatus.RUNNING
            step.started_at = now
            step.updated_at = now
            history = await session.scalar(
                select(ExecutionStepAttemptORM).where(
                    ExecutionStepAttemptORM.execution_attempt_id == attempt_id,
                    ExecutionStepAttemptORM.sequence == sequence,
                )
            )
            if history is None:
                session.add(
                    ExecutionStepAttemptORM(
                        execution_id=execution_id,
                        execution_attempt_id=attempt_id,
                        execution_step_id=step.id,
                        sequence=sequence,
                        skill_name=step.skill_name,
                        tool_name=step.tool_name,
                        input_parameters=step.input_parameters,
                        status=StepStatus.RUNNING,
                        outputs=[],
                        created_by_type=step.updated_by_type or step.created_by_type,
                        created_by=step.updated_by or step.created_by,
                        updated_by_type=step.updated_by_type or step.created_by_type,
                        updated_by=step.updated_by or step.created_by,
                        started_at=now,
                    )
                )
            else:
                history.status = StepStatus.RUNNING
                history.started_at = now
                history.finished_at = None
                history.error_message = None
                history.outputs = []
            if step.operation_id is None:
                raise ValueError(f"Execution Step {sequence} has no Operation.")
            await _add_outbox(
                session,
                execution_id,
                "execution.step_started",
                ExecutionStatus.RUNNING,
                {
                    "execution_attempt_id": str(attempt_id),
                    "operation_id": str(step.operation_id),
                    "step_id": str(step.id),
                    "sequence": sequence,
                    "status": StepStatus.RUNNING.value,
                },
            )

    async def _step_succeeded(
        self,
        execution_id: UUID,
        attempt_id: UUID,
        sequence: int,
        outputs: list[dict[str, Any]],
        execution_count: int | None,
    ) -> None:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            step = await session.scalar(
                select(ExecutionStepORM).where(
                    ExecutionStepORM.execution_id == execution_id,
                    ExecutionStepORM.sequence == sequence,
                )
            )
            if step is None or step.operation_id is None:
                raise ValueError(f"Execution Step {sequence} or its Operation was not found.")
            step.status = StepStatus.SUCCEEDED
            step.outputs = outputs
            step.finished_at = now
            step.updated_at = now
            await session.execute(
                update(ExecutionStepAttemptORM)
                .where(
                    ExecutionStepAttemptORM.execution_attempt_id == attempt_id,
                    ExecutionStepAttemptORM.sequence == sequence,
                )
                .values(
                    status=StepStatus.SUCCEEDED,
                    outputs=outputs,
                    finished_at=now,
                )
            )
            await _add_outbox(
                session,
                execution_id,
                "execution.step_succeeded",
                ExecutionStatus.RUNNING,
                {
                    "execution_attempt_id": str(attempt_id),
                    "operation_id": str(step.operation_id),
                    "step_id": str(step.id),
                    "sequence": sequence,
                    "status": StepStatus.SUCCEEDED.value,
                    "result": {
                        "outputs": outputs,
                        "execution_count": execution_count,
                    },
                },
            )

    async def _step_failed(
        self,
        execution_id: UUID,
        attempt_id: UUID,
        sequence: int,
        outputs: list[dict[str, Any]],
        error_message: str,
    ) -> None:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            step = await session.scalar(
                select(ExecutionStepORM).where(
                    ExecutionStepORM.execution_id == execution_id,
                    ExecutionStepORM.sequence == sequence,
                )
            )
            if step is None or step.operation_id is None:
                raise ValueError(f"Execution Step {sequence} or its Operation was not found.")
            safe_error = error_message[:2000]
            step.status = StepStatus.FAILED
            step.outputs = outputs
            step.error_message = safe_error
            step.finished_at = now
            step.updated_at = now
            await session.execute(
                update(ExecutionStepAttemptORM)
                .where(
                    ExecutionStepAttemptORM.execution_attempt_id == attempt_id,
                    ExecutionStepAttemptORM.sequence == sequence,
                )
                .values(
                    status=StepStatus.FAILED,
                    outputs=outputs,
                    error_message=safe_error,
                    finished_at=now,
                )
            )
            await _add_outbox(
                session,
                execution_id,
                "execution.step_failed",
                ExecutionStatus.RUNNING,
                {
                    "execution_attempt_id": str(attempt_id),
                    "operation_id": str(step.operation_id),
                    "step_id": str(step.id),
                    "sequence": sequence,
                    "status": StepStatus.FAILED.value,
                    "result": {"outputs": outputs, "execution_count": None},
                    "error_message": safe_error,
                },
            )

    async def _execute_runtime_step(
        self,
        driver: RuntimeDriver,
        runtime_session_id: str,
        code: str,
        execution_id: UUID,
        sequence: int,
    ) -> Any:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(
                        ExecutionStepORM.step_timeout_seconds,
                        ExecutionOperationORM.operation_timeout_seconds,
                        ExecutionOperationORM.started_at,
                    )
                    .join(
                        ExecutionOperationORM,
                        ExecutionOperationORM.id == ExecutionStepORM.operation_id,
                    )
                    .where(
                        ExecutionStepORM.execution_id == execution_id,
                        ExecutionStepORM.sequence == sequence,
                    )
                )
            ).one()
        timeouts: list[tuple[float, str]] = []
        if row.step_timeout_seconds is not None:
            timeouts.append((float(row.step_timeout_seconds), "Step"))
        if row.operation_timeout_seconds is not None:
            started_at = _as_utc(row.started_at or utc_now())
            remaining = row.operation_timeout_seconds - (utc_now() - started_at).total_seconds()
            if remaining <= 0:
                raise RuntimeExecutionTimeoutError(
                    "Operation", float(row.operation_timeout_seconds)
                )
            timeouts.append((remaining, "Operation"))
        if not timeouts:
            return await driver.execute(runtime_session_id, code)
        timeout_seconds, scope = min(timeouts)
        try:
            async with asyncio.timeout(timeout_seconds):
                return await driver.execute(runtime_session_id, code)
        except TimeoutError as exc:
            raise RuntimeExecutionTimeoutError(scope, timeout_seconds) from exc

    async def _heartbeat(self, execution_id: UUID, attempt_id: UUID) -> None:
        while True:
            await asyncio.sleep(self._settings.execution_heartbeat_seconds)
            now = utc_now()
            lease = now + timedelta(seconds=self._settings.execution_lease_seconds)
            async with self._session_factory() as session, session.begin():
                await session.execute(
                    update(ExecutionORM)
                    .where(
                        ExecutionORM.id == execution_id,
                        ExecutionORM.status == ExecutionStatus.RUNNING,
                    )
                    .values(heartbeat_at=now, lease_expires_at=lease, updated_at=now)
                )
                await session.execute(
                    update(ExecutionAttemptORM)
                    .where(
                        ExecutionAttemptORM.id == attempt_id,
                        ExecutionAttemptORM.status == AttemptStatus.RUNNING,
                    )
                    .values(heartbeat_at=now, lease_expires_at=lease)
                )

    async def _record_artifact_failure(
        self,
        execution_id: UUID,
        attempt_id: UUID,
        sequence: int,
        error: Exception,
    ) -> None:
        try:
            async with self._session_factory() as session, session.begin():
                await _add_outbox(
                    session,
                    execution_id,
                    "execution.artifact_failed",
                    ExecutionStatus.RUNNING,
                    {
                        "execution_attempt_id": str(attempt_id),
                        "sequence": sequence,
                        "error_type": type(error).__name__,
                    },
                )
        except Exception:
            # Never replace the original execution or Artifact error with telemetry failure.
            logger.exception(
                "Artifact failure event persistence failed",
                extra={"execution_id": str(execution_id)},
            )

    async def _finalize(
        self,
        execution_id: UUID,
        attempt_id: UUID,
        requested_status: ExecutionStatus,
        error_message: str | None = None,
        *,
        retain_session: bool = False,
        retry_from_sequence: int | None = None,
        failure_type: FailureType | None = None,
        retry_strategy: RetryStrategy = RetryStrategy.NOT_RETRYABLE,
        runtime_session_cleanup_status: RuntimeSessionCleanupStatus = (
            RuntimeSessionCleanupStatus.NOT_REQUIRED
        ),
    ) -> None:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            execution = await session.scalar(
                select(ExecutionORM).where(ExecutionORM.id == execution_id).with_for_update()
            )
            if (
                execution is None
                or execution.status.is_terminal
                or execution.status == ExecutionStatus.CANCEL_REQUESTED
            ):
                return
            status = requested_status
            attempt_status = AttemptStatus(status.value)
            is_failed = status == ExecutionStatus.FAILED
            effective_failure_type = failure_type if is_failed else None
            effective_retry_strategy = retry_strategy if is_failed else RetryStrategy.NOT_RETRYABLE
            execution.status = status
            execution.error_message = error_message if is_failed else None
            execution.failure_type = effective_failure_type
            execution.finished_at = now
            execution.updated_at = now
            execution.lease_owner = None
            execution.lease_expires_at = None
            execution.operation_wait_expires_at = None
            execution.retry_strategy = effective_retry_strategy
            execution.retry_from_sequence = None
            if effective_retry_strategy == RetryStrategy.FROM_FAILED_STEP:
                execution.retry_from_sequence = retry_from_sequence
            elif effective_retry_strategy == RetryStrategy.FROM_START:
                execution.retry_from_sequence = 0
            execution.retained_runtime_session_until = (
                now + timedelta(seconds=self._settings.failed_session_retention_seconds)
                if is_failed and retain_session
                else None
            )
            execution.runtime_session_cleanup_status = runtime_session_cleanup_status
            if (
                not retain_session
                and runtime_session_cleanup_status == RuntimeSessionCleanupStatus.SUCCEEDED
            ):
                execution.runtime_session_id = None
            execution.version += 1
            await session.execute(
                update(ExecutionAttemptORM)
                .where(ExecutionAttemptORM.id == attempt_id)
                .values(
                    status=attempt_status,
                    error_message=error_message if is_failed else None,
                    failure_type=effective_failure_type,
                    retry_strategy=effective_retry_strategy,
                    runtime_session_cleanup_status=runtime_session_cleanup_status,
                    finished_at=now,
                )
            )
            if execution.active_operation_id is not None:
                operation_update = await session.execute(
                    update(ExecutionOperationORM)
                    .where(
                        ExecutionOperationORM.id == execution.active_operation_id,
                        ExecutionOperationORM.status.in_(
                            [OperationStatus.QUEUED, OperationStatus.RUNNING]
                        ),
                    )
                    .values(
                        status=(
                            OperationStatus.SUCCEEDED
                            if status == ExecutionStatus.SUCCEEDED
                            else OperationStatus.FAILED
                        ),
                        execution_attempt_id=attempt_id,
                        error_message=error_message if is_failed else None,
                        finished_at=now,
                        updated_at=now,
                    )
                )
                operation = await session.scalar(
                    select(ExecutionOperationORM)
                    .where(ExecutionOperationORM.id == execution.active_operation_id)
                    .execution_options(populate_existing=True)
                )
                if operation is not None and getattr(operation_update, "rowcount", None) == 1:
                    operation_payload: dict[str, object] = {
                        "execution_attempt_id": str(attempt_id),
                        "operation_id": str(operation.id),
                        "operation_status": (
                            OperationStatus.SUCCEEDED.value
                            if status == ExecutionStatus.SUCCEEDED
                            else OperationStatus.FAILED.value
                        ),
                        "first_sequence": operation.first_sequence,
                        "last_sequence": operation.last_sequence,
                        "version": execution.version,
                    }
                    if status == ExecutionStatus.FAILED:
                        operation_payload["failed_sequence"] = retry_from_sequence
                        operation_payload["error_message"] = error_message or "Operation failed."
                    await _add_outbox(
                        session,
                        execution_id,
                        (
                            "execution.operation_succeeded"
                            if status == ExecutionStatus.SUCCEEDED
                            else "execution.operation_failed"
                        ),
                        status,
                        operation_payload,
                    )
            if status == ExecutionStatus.FAILED:
                await session.execute(
                    update(ExecutionStepORM)
                    .where(
                        ExecutionStepORM.execution_id == execution_id,
                        ExecutionStepORM.status == StepStatus.RUNNING,
                    )
                    .values(
                        status=StepStatus.FAILED,
                        error_message=error_message,
                        finished_at=now,
                        updated_at=now,
                    )
                )
                await session.execute(
                    update(ExecutionStepORM)
                    .where(
                        ExecutionStepORM.execution_id == execution_id,
                        ExecutionStepORM.status == StepStatus.PENDING,
                    )
                    .values(status=StepStatus.SKIPPED, finished_at=now, updated_at=now)
                )
                await session.execute(
                    update(ExecutionStepAttemptORM)
                    .where(
                        ExecutionStepAttemptORM.execution_attempt_id == attempt_id,
                        ExecutionStepAttemptORM.status == StepStatus.RUNNING,
                    )
                    .values(
                        status=StepStatus.FAILED,
                        error_message=error_message,
                        finished_at=now,
                    )
                )
            await _add_outbox(
                session,
                execution_id,
                f"execution.{status.value.lower()}",
                status,
                {
                    "failure_type": (
                        effective_failure_type.value if effective_failure_type else None
                    ),
                    "retry_strategy": effective_retry_strategy.value,
                    "retry_from_sequence": execution.retry_from_sequence,
                    "runtime_session_cleanup_status": runtime_session_cleanup_status.value,
                },
            )

    async def _cancel_execution(self, execution_id: UUID) -> None:
        target: RuntimeTargetORM | None = None
        runtime_session_id: str | None = None
        cleanup_status = RuntimeSessionCleanupStatus.NOT_REQUIRED
        async with self._session_factory() as session:
            execution = await session.get(ExecutionORM, execution_id)
            if execution is None or execution.status != ExecutionStatus.CANCEL_REQUESTED:
                return
            runtime_session_id = execution.runtime_session_id
            if execution.runtime_target_id is not None:
                target = await session.get(RuntimeTargetORM, execution.runtime_target_id)
        if target is not None and runtime_session_id is not None:
            driver = self._create_driver(target)
            try:
                cleanup_status = await _best_effort_session_stop(driver, runtime_session_id)
            finally:
                await driver.close()
        elif runtime_session_id is not None:
            cleanup_status = RuntimeSessionCleanupStatus.FAILED
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            execution = await session.scalar(
                select(ExecutionORM).where(ExecutionORM.id == execution_id).with_for_update()
            )
            if execution is None or execution.status != ExecutionStatus.CANCEL_REQUESTED:
                return
            execution.status = ExecutionStatus.CANCELLED
            execution.finished_at = now
            execution.updated_at = now
            execution.lease_owner = None
            execution.lease_expires_at = None
            execution.operation_wait_expires_at = None
            execution.failure_type = None
            execution.retry_strategy = RetryStrategy.NOT_RETRYABLE
            execution.retry_from_sequence = None
            execution.retained_runtime_session_until = None
            execution.runtime_session_cleanup_status = cleanup_status
            if cleanup_status == RuntimeSessionCleanupStatus.SUCCEEDED:
                execution.runtime_session_id = None
            execution.version += 1
            await session.execute(
                update(ExecutionAttemptORM)
                .where(
                    ExecutionAttemptORM.execution_id == execution_id,
                    ExecutionAttemptORM.status.in_([AttemptStatus.RUNNING, AttemptStatus.WAITING]),
                )
                .values(
                    status=AttemptStatus.CANCELLED,
                    failure_type=None,
                    retry_strategy=RetryStrategy.NOT_RETRYABLE,
                    runtime_session_cleanup_status=cleanup_status,
                    finished_at=now,
                )
            )
            await session.execute(
                update(ExecutionStepORM)
                .where(
                    ExecutionStepORM.execution_id == execution_id,
                    ExecutionStepORM.status.in_([StepStatus.PENDING, StepStatus.RUNNING]),
                )
                .values(status=StepStatus.CANCELLED, finished_at=now, updated_at=now)
            )
            await session.execute(
                update(ExecutionOperationORM)
                .where(
                    ExecutionOperationORM.execution_id == execution_id,
                    ExecutionOperationORM.status.in_(
                        [OperationStatus.QUEUED, OperationStatus.RUNNING]
                    ),
                )
                .values(status=OperationStatus.CANCELLED, finished_at=now, updated_at=now)
            )
            await session.execute(
                update(ExecutionStepAttemptORM)
                .where(
                    ExecutionStepAttemptORM.execution_id == execution_id,
                    ExecutionStepAttemptORM.status == StepStatus.RUNNING,
                )
                .values(status=StepStatus.CANCELLED, finished_at=now)
            )
            await _add_outbox(
                session,
                execution_id,
                "execution.cancelled",
                ExecutionStatus.CANCELLED,
                {"runtime_session_cleanup_status": cleanup_status.value},
            )

    async def _audit_multi_lifecycle(self) -> None:
        await self._request_expired_execution_cancellations()
        now = utc_now()
        async with self._session_factory() as session:
            waiting = list(
                await session.execute(
                    select(ExecutionORM, RuntimeTargetORM)
                    .join(
                        RuntimeTargetORM,
                        RuntimeTargetORM.id == ExecutionORM.runtime_target_id,
                    )
                    .where(
                        ExecutionORM.operation_mode == OperationMode.MULTI,
                        ExecutionORM.status == ExecutionStatus.WAITING_FOR_OPERATION,
                    )
                    .order_by(ExecutionORM.updated_at)
                    .limit(200)
                )
            )
        for execution, target in waiting:
            if (
                execution.execution_expires_at is not None
                and _as_utc(execution.execution_expires_at) <= now
            ):
                await self._fail_waiting_execution(
                    execution.id,
                    execution.runtime_session_id,
                    FailureType.EXECUTION_TIMEOUT,
                    "Execution exceeded its maximum runtime while waiting for the Agent.",
                )
                continue
            if (
                execution.operation_wait_expires_at is not None
                and _as_utc(execution.operation_wait_expires_at) <= now
            ):
                await self._fail_waiting_execution(
                    execution.id,
                    execution.runtime_session_id,
                    FailureType.OPERATION_WAIT_TIMEOUT,
                    "The next Operation was not provided before the wait deadline.",
                )
                continue
            if not target.enabled:
                await self._fail_waiting_execution(
                    execution.id,
                    execution.runtime_session_id,
                    FailureType.RUNTIME_UNAVAILABLE,
                    "The assigned Runtime Target was disabled while waiting for the Agent.",
                )
                continue
            if execution.runtime_session_id is None:
                await self._fail_waiting_execution(
                    execution.id,
                    None,
                    FailureType.RUNTIME_SESSION_LOST,
                    "The retained MULTI Runtime session reference was lost.",
                )
                continue
            driver = self._create_driver(target)
            try:
                session_exists = await driver.session_exists(execution.runtime_session_id)
            except RuntimeDriverError:
                # OFFLINE can be temporary. The persisted deadlines remain the terminal guard.
                continue
            finally:
                await driver.close()
            if not session_exists:
                await self._fail_waiting_execution(
                    execution.id,
                    execution.runtime_session_id,
                    FailureType.RUNTIME_SESSION_LOST,
                    "The retained MULTI Runtime session no longer exists.",
                )

    async def _request_expired_execution_cancellations(self) -> None:
        now = utc_now()
        expired_ids: list[UUID] = []
        async with self._session_factory() as session, session.begin():
            expired = list(
                await session.scalars(
                    select(ExecutionORM)
                    .where(
                        ExecutionORM.status.in_([ExecutionStatus.QUEUED, ExecutionStatus.RUNNING]),
                        ExecutionORM.execution_expires_at.is_not(None),
                        ExecutionORM.execution_expires_at <= now,
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            for execution in expired:
                execution.status = ExecutionStatus.CANCEL_REQUESTED
                execution.cancellation_reason = "Execution exceeded its maximum runtime."
                execution.operation_wait_expires_at = None
                execution.updated_at = now
                execution.version += 1
                expired_ids.append(execution.id)
                await _add_outbox(
                    session,
                    execution.id,
                    "execution.timeout_requested",
                    ExecutionStatus.CANCEL_REQUESTED,
                    {"failure_type": FailureType.EXECUTION_TIMEOUT.value},
                )
        for execution_id in expired_ids:
            self._dispatch(
                execution_id,
                self._cancel_execution(execution_id),
                replace=True,
            )

    async def _fail_waiting_execution(
        self,
        execution_id: UUID,
        expected_runtime_session_id: str | None,
        failure_type: FailureType,
        error_message: str,
    ) -> None:
        now = utc_now()
        cleanup_target: tuple[UUID, UUID | None, UUID, str] | None = None
        async with self._session_factory() as session, session.begin():
            execution = await session.scalar(
                select(ExecutionORM).where(ExecutionORM.id == execution_id).with_for_update()
            )
            if (
                execution is None
                or execution.status != ExecutionStatus.WAITING_FOR_OPERATION
                or execution.runtime_session_id != expected_runtime_session_id
            ):
                return
            attempt = await session.scalar(
                select(ExecutionAttemptORM)
                .where(
                    ExecutionAttemptORM.execution_id == execution_id,
                    ExecutionAttemptORM.status == AttemptStatus.WAITING,
                )
                .with_for_update()
            )
            cleanup_required = (
                failure_type != FailureType.RUNTIME_SESSION_LOST
                and execution.runtime_session_id is not None
                and execution.runtime_target_id is not None
            )
            cleanup_status = (
                RuntimeSessionCleanupStatus.PENDING
                if cleanup_required
                else RuntimeSessionCleanupStatus.NOT_REQUIRED
            )
            execution.status = ExecutionStatus.FAILED
            execution.error_message = error_message
            execution.failure_type = failure_type
            execution.finished_at = now
            execution.updated_at = now
            execution.lease_owner = None
            execution.lease_expires_at = None
            execution.operation_wait_expires_at = None
            execution.retry_strategy = RetryStrategy.NOT_RETRYABLE
            execution.retry_from_sequence = None
            execution.retained_runtime_session_until = None
            execution.runtime_session_cleanup_status = cleanup_status
            execution.recovery_count += 1
            execution.version += 1
            if failure_type == FailureType.RUNTIME_SESSION_LOST:
                execution.runtime_session_id = None
            if attempt is not None:
                attempt.status = AttemptStatus.FAILED
                attempt.lease_owner = None
                attempt.lease_expires_at = None
                attempt.error_message = error_message
                attempt.failure_type = failure_type
                attempt.retry_strategy = RetryStrategy.NOT_RETRYABLE
                attempt.runtime_session_cleanup_status = cleanup_status
                attempt.finished_at = now
            if cleanup_required:
                if execution.runtime_target_id is None or expected_runtime_session_id is None:
                    raise RuntimeError("Dynamic cleanup target unexpectedly missing.")
                cleanup_target = (
                    execution.id,
                    attempt.id if attempt is not None else None,
                    execution.runtime_target_id,
                    expected_runtime_session_id,
                )
            await _add_outbox(
                session,
                execution.id,
                "execution.failed",
                ExecutionStatus.FAILED,
                {
                    "failure_type": failure_type.value,
                    "retry_strategy": RetryStrategy.NOT_RETRYABLE.value,
                    "runtime_session_cleanup_status": cleanup_status.value,
                    "recovery_count": execution.recovery_count,
                },
            )
        if cleanup_target is not None:
            await self._cleanup_abandoned_session(*cleanup_target)

    async def _recover_expired_leases(self) -> None:
        now = utc_now()
        cleanup_targets: list[tuple[UUID, UUID, UUID, str]] = []
        async with self._session_factory() as session, session.begin():
            expired = list(
                await session.scalars(
                    select(ExecutionORM)
                    .where(
                        ExecutionORM.status == ExecutionStatus.RUNNING,
                        ExecutionORM.lease_expires_at < now,
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            for execution in expired:
                retry_strategy = (
                    RetryStrategy.NOT_RETRYABLE
                    if execution.operation_mode == OperationMode.MULTI
                    else RetryStrategy.FROM_START
                )
                attempt = await session.scalar(
                    select(ExecutionAttemptORM)
                    .where(
                        ExecutionAttemptORM.execution_id == execution.id,
                        ExecutionAttemptORM.status == AttemptStatus.RUNNING,
                    )
                    .with_for_update()
                )
                if (
                    execution.runtime_target_id is not None
                    and execution.runtime_session_id is not None
                    and attempt is not None
                ):
                    cleanup_targets.append(
                        (
                            execution.id,
                            attempt.id,
                            execution.runtime_target_id,
                            execution.runtime_session_id,
                        )
                    )
                execution.status = ExecutionStatus.FAILED
                execution.error_message = "Worker lease expired; execution requires retry."
                execution.failure_type = FailureType.LEASE_EXPIRED
                execution.finished_at = now
                execution.updated_at = now
                execution.lease_owner = None
                execution.lease_expires_at = None
                execution.retry_strategy = retry_strategy
                execution.retry_from_sequence = (
                    0 if retry_strategy == RetryStrategy.FROM_START else None
                )
                execution.retained_runtime_session_until = None
                execution.recovery_count += 1
                execution.runtime_session_cleanup_status = (
                    RuntimeSessionCleanupStatus.PENDING
                    if cleanup_targets and cleanup_targets[-1][0] == execution.id
                    else RuntimeSessionCleanupStatus.NOT_REQUIRED
                )
                execution.version += 1
                await session.execute(
                    update(ExecutionAttemptORM)
                    .where(
                        ExecutionAttemptORM.execution_id == execution.id,
                        ExecutionAttemptORM.status == AttemptStatus.RUNNING,
                    )
                    .values(
                        status=AttemptStatus.FAILED,
                        error_message=execution.error_message,
                        failure_type=FailureType.LEASE_EXPIRED,
                        retry_strategy=retry_strategy,
                        runtime_session_cleanup_status=execution.runtime_session_cleanup_status,
                        finished_at=now,
                    )
                )
                await session.execute(
                    update(ExecutionStepORM)
                    .where(
                        ExecutionStepORM.execution_id == execution.id,
                        ExecutionStepORM.status == StepStatus.RUNNING,
                    )
                    .values(
                        status=StepStatus.FAILED,
                        error_message=execution.error_message,
                        finished_at=now,
                        updated_at=now,
                    )
                )
                await session.execute(
                    update(ExecutionStepAttemptORM)
                    .where(
                        ExecutionStepAttemptORM.execution_id == execution.id,
                        ExecutionStepAttemptORM.status == StepStatus.RUNNING,
                    )
                    .values(
                        status=StepStatus.FAILED,
                        error_message=execution.error_message,
                        finished_at=now,
                    )
                )
                await session.execute(
                    update(ExecutionStepORM)
                    .where(
                        ExecutionStepORM.execution_id == execution.id,
                        ExecutionStepORM.status == StepStatus.PENDING,
                    )
                    .values(status=StepStatus.SKIPPED, finished_at=now, updated_at=now)
                )
                if execution.active_operation_id is not None:
                    operation = await session.scalar(
                        select(ExecutionOperationORM)
                        .where(
                            ExecutionOperationORM.id == execution.active_operation_id,
                            ExecutionOperationORM.status.in_(
                                [OperationStatus.QUEUED, OperationStatus.RUNNING]
                            ),
                        )
                        .with_for_update()
                    )
                    if operation is not None:
                        operation.status = OperationStatus.FAILED
                        if attempt is not None:
                            operation.execution_attempt_id = attempt.id
                        operation.error_message = execution.error_message
                        operation.finished_at = now
                        operation.updated_at = now
                        await _add_outbox(
                            session,
                            execution.id,
                            "execution.operation_failed",
                            ExecutionStatus.FAILED,
                            {
                                "execution_attempt_id": (
                                    str(operation.execution_attempt_id)
                                    if operation.execution_attempt_id is not None
                                    else None
                                ),
                                "operation_id": str(operation.id),
                                "operation_status": OperationStatus.FAILED.value,
                                "first_sequence": operation.first_sequence,
                                "last_sequence": operation.last_sequence,
                                "version": execution.version,
                                "error_message": execution.error_message
                                or "Operation recovery failed.",
                            },
                        )
                await _add_outbox(
                    session,
                    execution.id,
                    "execution.failed",
                    ExecutionStatus.FAILED,
                    {
                        "failure_type": FailureType.LEASE_EXPIRED.value,
                        "retry_strategy": retry_strategy.value,
                        "retry_from_sequence": execution.retry_from_sequence,
                        "runtime_session_cleanup_status": (
                            execution.runtime_session_cleanup_status.value
                        ),
                        "recovery_count": execution.recovery_count,
                    },
                )
        for execution_id, attempt_id, target_id, runtime_session_id in cleanup_targets:
            await self._cleanup_abandoned_session(
                execution_id, attempt_id, target_id, runtime_session_id
            )

    async def _cleanup_abandoned_session(
        self,
        execution_id: UUID,
        attempt_id: UUID | None,
        target_id: UUID,
        runtime_session_id: str,
    ) -> None:
        async with self._session_factory() as session:
            target = await session.get(RuntimeTargetORM, target_id)
        if target is None:
            await self._record_cleanup_result(
                execution_id, attempt_id, runtime_session_id, RuntimeSessionCleanupStatus.FAILED
            )
            return
        driver = self._create_driver(target)
        try:
            await driver.delete_session(runtime_session_id)
        except Exception:
            logger.warning(
                "Abandoned runtime session cleanup failed",
                extra={"execution_id": str(execution_id)},
            )
            cleanup_status = RuntimeSessionCleanupStatus.FAILED
        else:
            cleanup_status = RuntimeSessionCleanupStatus.SUCCEEDED
        finally:
            await driver.close()
        await self._record_cleanup_result(
            execution_id, attempt_id, runtime_session_id, cleanup_status
        )

    async def _record_cleanup_result(
        self,
        execution_id: UUID,
        attempt_id: UUID | None,
        runtime_session_id: str,
        cleanup_status: RuntimeSessionCleanupStatus,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            execution_update = await session.execute(
                update(ExecutionORM)
                .where(
                    ExecutionORM.id == execution_id,
                    ExecutionORM.status == ExecutionStatus.FAILED,
                    ExecutionORM.runtime_session_id == runtime_session_id,
                )
                .values(
                    runtime_session_id=(
                        None
                        if cleanup_status == RuntimeSessionCleanupStatus.SUCCEEDED
                        else runtime_session_id
                    ),
                    runtime_session_cleanup_status=cleanup_status,
                    updated_at=utc_now(),
                    version=ExecutionORM.version + 1,
                )
            )
            if attempt_id is not None:
                await session.execute(
                    update(ExecutionAttemptORM)
                    .where(ExecutionAttemptORM.id == attempt_id)
                    .values(runtime_session_cleanup_status=cleanup_status)
                )
            if getattr(execution_update, "rowcount", None) == 1:
                await _add_outbox(
                    session,
                    execution_id,
                    (
                        "execution.runtime_session_cleanup_completed"
                        if cleanup_status == RuntimeSessionCleanupStatus.SUCCEEDED
                        else "execution.runtime_session_cleanup_failed"
                    ),
                    ExecutionStatus.FAILED,
                    {"runtime_session_cleanup_status": cleanup_status.value},
                )

    async def _cleanup_expired_retained_runtime_sessions(self) -> None:
        now = utc_now()
        async with self._session_factory() as session:
            rows = list(
                await session.execute(
                    select(ExecutionORM, RuntimeTargetORM)
                    .join(
                        RuntimeTargetORM,
                        RuntimeTargetORM.id == ExecutionORM.runtime_target_id,
                    )
                    .where(
                        ExecutionORM.status.in_([ExecutionStatus.FAILED, ExecutionStatus.QUEUED]),
                        ExecutionORM.retry_strategy == RetryStrategy.FROM_FAILED_STEP,
                        ExecutionORM.retained_runtime_session_until <= now,
                        ExecutionORM.runtime_session_id.is_not(None),
                    )
                )
            )
        for execution, target in rows:
            driver = self._create_driver(target)
            cleanup_status = RuntimeSessionCleanupStatus.SUCCEEDED
            try:
                if execution.runtime_session_id is not None:
                    await driver.delete_session(execution.runtime_session_id)
            except Exception:
                cleanup_status = RuntimeSessionCleanupStatus.FAILED
                logger.warning(
                    "Expired retained runtime session cleanup failed",
                    extra={"execution_id": str(execution.id)},
                )
            finally:
                await driver.close()
            async with self._session_factory() as update_session, update_session.begin():
                current = await update_session.scalar(
                    select(ExecutionORM).where(ExecutionORM.id == execution.id).with_for_update()
                )
                if (
                    current is None
                    or current.status not in {ExecutionStatus.FAILED, ExecutionStatus.QUEUED}
                    or current.retry_strategy != RetryStrategy.FROM_FAILED_STEP
                    or current.retained_runtime_session_until is None
                    or _as_utc(current.retained_runtime_session_until) > now
                ):
                    continue
                retry_was_queued = current.status == ExecutionStatus.QUEUED
                if retry_was_queued:
                    current.status = ExecutionStatus.FAILED
                    current.error_message = (
                        "The retained runtime session retry window expired before "
                        "execution resumed."
                    )
                    current.finished_at = now
                    await update_session.execute(
                        update(ExecutionStepORM)
                        .where(
                            ExecutionStepORM.execution_id == current.id,
                            ExecutionStepORM.status == StepStatus.PENDING,
                        )
                        .values(status=StepStatus.SKIPPED, finished_at=now, updated_at=now)
                    )
                current.retry_strategy = RetryStrategy.NOT_RETRYABLE
                current.retry_from_sequence = None
                current.retained_runtime_session_until = None
                if cleanup_status == RuntimeSessionCleanupStatus.SUCCEEDED:
                    current.runtime_session_id = None
                current.runtime_session_cleanup_status = cleanup_status
                current.updated_at = now
                current.version += 1
                if retry_was_queued:
                    await self._fail_active_operation_without_attempt(
                        update_session,
                        current,
                        now,
                        current.error_message
                        or "The retained Runtime session retry window expired.",
                    )
                latest_attempt_id = await update_session.scalar(
                    select(ExecutionAttemptORM.id)
                    .where(ExecutionAttemptORM.execution_id == current.id)
                    .order_by(ExecutionAttemptORM.attempt_number.desc())
                    .limit(1)
                )
                if latest_attempt_id is not None:
                    await update_session.execute(
                        update(ExecutionAttemptORM)
                        .where(ExecutionAttemptORM.id == latest_attempt_id)
                        .values(runtime_session_cleanup_status=cleanup_status)
                    )
                await _add_outbox(
                    update_session,
                    current.id,
                    "execution.retry_window_expired",
                    ExecutionStatus.FAILED,
                    {
                        "runtime_session_cleanup_status": cleanup_status.value,
                        "retry_was_queued": retry_was_queued,
                    },
                )

    async def _fail_active_operation_without_attempt(
        self,
        session: AsyncSession,
        execution: ExecutionORM,
        now: datetime,
        error_message: str,
    ) -> None:
        if execution.active_operation_id is None:
            return
        operation = await session.scalar(
            select(ExecutionOperationORM)
            .where(
                ExecutionOperationORM.id == execution.active_operation_id,
                ExecutionOperationORM.status.in_([OperationStatus.QUEUED, OperationStatus.RUNNING]),
            )
            .with_for_update()
        )
        if operation is None:
            return
        operation.status = OperationStatus.FAILED
        operation.execution_attempt_id = None
        operation.error_message = error_message[:2000]
        operation.finished_at = now
        operation.updated_at = now
        await _add_outbox(
            session,
            execution.id,
            "execution.operation_failed",
            ExecutionStatus.FAILED,
            {
                "execution_attempt_id": None,
                "operation_id": str(operation.id),
                "operation_status": OperationStatus.FAILED.value,
                "first_sequence": operation.first_sequence,
                "last_sequence": operation.last_sequence,
                "version": execution.version,
                "error_message": operation.error_message or "Operation failed.",
            },
        )


async def _add_outbox(
    session: AsyncSession,
    execution_id: UUID,
    event_type: str,
    status: ExecutionStatus,
    details: dict[str, object] | None = None,
) -> None:
    actor_row = (
        await session.execute(
            select(
                ExecutionORM.updated_by_type,
                ExecutionORM.updated_by,
                ExecutionORM.created_by_type,
                ExecutionORM.created_by,
            ).where(ExecutionORM.id == execution_id)
        )
    ).one_or_none()
    actor_type = None
    actor_id = None
    if actor_row is not None:
        actor_type = actor_row.updated_by_type or actor_row.created_by_type
        actor_id = actor_row.updated_by or actor_row.created_by
    payload: dict[str, object] = {
        "execution_id": str(execution_id),
        "status": status.value,
    }
    if details:
        payload.update(details)
    carrier = capture_trace_carrier()
    event = build_execution_event(
        execution_id=execution_id,
        event_type=event_type,
        payload=payload,
        actor_type=actor_type,
        actor_id=actor_id,
        traceparent=carrier.traceparent,
        tracestate=carrier.tracestate,
    )
    session.add(OutboxEventORM.from_domain(event))


def _invalid_work_message_reason(fields: dict[str, str]) -> str | None:
    message_id = fields.get("message_id")
    if not message_id:
        return "missing_message_id"
    try:
        UUID(message_id)
    except ValueError:
        return "invalid_message_id"
    if fields.get("aggregate_type") != "Execution":
        return "unsupported_aggregate_type"
    aggregate_id = fields.get("aggregate_id")
    if not aggregate_id:
        return "missing_aggregate_id"
    try:
        UUID(aggregate_id)
    except ValueError:
        return "invalid_aggregate_id"
    message_type = fields.get("message_type")
    if not message_type:
        return "missing_message_type"
    if message_type not in DISPATCH_MESSAGE_TYPES:
        return "unsupported_message_type"
    schema_version = fields.get("schema_version")
    if not schema_version:
        return "missing_schema_version"
    if schema_version != WORK_MESSAGE_SCHEMA_VERSION:
        return "unsupported_schema_version"
    if not fields.get("payload"):
        return "missing_payload"
    try:
        WorkStreamEnvelope.from_redis_fields(fields)
    except (TypeError, ValueError):
        return "invalid_work_message_contract"
    return None


def _valid_uuid_or_empty(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(UUID(value))
    except ValueError:
        return ""


def _failure_policy(exc: Exception, retain_session: bool) -> tuple[FailureType, RetryStrategy]:
    if isinstance(exc, RetainedRuntimeSessionLostError):
        return FailureType.RUNTIME_SESSION_LOST, RetryStrategy.FROM_START
    if isinstance(exc, RuntimeExecutionTimeoutError):
        failure_type = (
            FailureType.STEP_TIMEOUT if exc.scope == "Step" else FailureType.OPERATION_TIMEOUT
        )
        retry_strategy = (
            RetryStrategy.FROM_FAILED_STEP if retain_session else RetryStrategy.FROM_START
        )
        return failure_type, retry_strategy
    if isinstance(exc, RuntimeExecutionError) and retain_session:
        return FailureType.TOOL_ERROR, RetryStrategy.FROM_FAILED_STEP
    if isinstance(exc, RuntimeDriverError):
        return FailureType.RUNTIME_UNAVAILABLE, RetryStrategy.FROM_START
    return FailureType.INTERNAL_ERROR, RetryStrategy.NOT_RETRYABLE


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, (RuntimeExecutionError, RuntimeDriverError)):
        return str(exc)[:2000]
    return f"{type(exc).__name__}: execution failed"[:2000]


async def _best_effort_session_stop(
    driver: RuntimeDriver, runtime_session_id: str
) -> RuntimeSessionCleanupStatus:
    try:
        await driver.interrupt_session(runtime_session_id)
        await driver.delete_session(runtime_session_id)
    except Exception:
        logger.warning(
            "Runtime session cleanup failed", extra={"runtime_session_id": runtime_session_id}
        )
        return RuntimeSessionCleanupStatus.FAILED
    return RuntimeSessionCleanupStatus.SUCCEEDED


def _as_utc(value: datetime) -> datetime:
    """SQLite tests may return timezone-naive values for aware columns."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
