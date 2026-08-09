"""Redis-triggered Jupyter execution worker with PostgreSQL leases."""

import asyncio
import logging
import os
import socket
from collections.abc import Awaitable, Coroutine
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
    ExecutionMode,
    ExecutionStatus,
    FailureType,
    JupyterServerStatus,
    KernelCleanupStatus,
    RetryStrategy,
    StepStatus,
)
from executor_service.domain.models import Execution, OutboxEvent, utc_now
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionORM,
    ExecutionStepAttemptORM,
    ExecutionStepORM,
    JupyterServerORM,
    OutboxEventORM,
)
from executor_service.infrastructure.jupyter import (
    JupyterExecutionError,
    JupyterGateway,
    JupyterGatewayError,
)
from executor_service.infrastructure.jupyter_registry import JupyterServerRegistry
from executor_service.infrastructure.workspace import ExecutionWorkspace, WorkspaceManager
from executor_service.tracing import (
    TracingManager,
    capture_trace_carrier,
    extract_trace_context,
)

logger = logging.getLogger(__name__)


class ExecutionWorker:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis,
        settings: Settings,
        registry: JupyterServerRegistry,
        artifact_manager: ExecutionArtifactManager,
        tracing: TracingManager | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._redis = redis
        self._settings = settings
        self._registry = registry
        self._artifacts = artifact_manager
        self._tracing = tracing or TracingManager(settings)
        self._workspace = WorkspaceManager(settings.workspace_host_root)
        self._consumer_name = settings.execution_consumer_name or (
            f"{socket.gethostname()}-{os.getpid()}"
        )
        self._stop_event = asyncio.Event()
        self._loops: list[asyncio.Task[None]] = []
        self._jobs: dict[UUID, asyncio.Task[None]] = {}
        self._semaphore = asyncio.Semaphore(settings.execution_worker_concurrency)

    async def start(self) -> None:
        if not self._settings.jupyter_enabled or self._loops:
            return
        self._settings.workspace_host_root.mkdir(parents=True, exist_ok=True)
        await self._ensure_consumer_group()
        self._loops = [
            asyncio.create_task(self._stream_loop(), name="execution-stream-consumer"),
            asyncio.create_task(self._reconcile_loop(), name="execution-reconciler"),
            asyncio.create_task(self._lease_recovery_loop(), name="execution-lease-recovery"),
            asyncio.create_task(
                self._retained_kernel_cleanup_loop(),
                name="retained-kernel-cleanup",
            ),
            asyncio.create_task(
                self._dynamic_lifecycle_loop(),
                name="dynamic-lifecycle-auditor",
            ),
        ]

    async def stop(self) -> None:
        self._stop_event.set()
        for task in self._loops:
            task.cancel()
        await asyncio.gather(*self._loops, return_exceptions=True)
        self._loops.clear()
        if self._jobs:
            for task in self._jobs.values():
                task.cancel()
            await asyncio.gather(*self._jobs.values(), return_exceptions=True)

    async def _ensure_consumer_group(self) -> None:
        try:
            await self._redis.xgroup_create(
                self._settings.redis_stream,
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
                    streams={self._settings.redis_stream: ">"},
                    count=20,
                    block=1000,
                )
                for _stream, messages in batches:
                    for message_id, fields in messages:
                        await self._handle_event(fields)
                        await self._redis.xack(
                            self._settings.redis_stream,
                            self._settings.execution_consumer_group,
                            message_id,
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Execution stream consumer failed")
                await asyncio.sleep(1)

    async def _handle_event(self, fields: dict[str, str]) -> None:
        event_type = fields.get("event_type")
        try:
            execution_id = UUID(fields["aggregate_id"])
        except (KeyError, ValueError):
            logger.warning("Ignoring malformed execution event")
            return
        context = extract_trace_context(fields)
        with self._tracing.span(
            "executor.redis.consume",
            context=context,
            kind=SpanKind.CONSUMER,
            attributes={
                "executor.event.type": event_type,
                "executor.execution.id": str(execution_id),
            },
        ):
            if event_type in {
                "execution.submitted",
                "execution.continue_requested",
                "execution.finish_requested",
            }:
                self._dispatch(execution_id, self._run_execution(execution_id))
            elif event_type == "execution.retry_requested":
                self._dispatch(execution_id, self._run_execution(execution_id))
            elif event_type == "execution.cancel_requested":
                self._dispatch(
                    execution_id,
                    self._cancel_execution(execution_id),
                    replace=True,
                )

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
                                    [ExecutionStatus.QUEUED, ExecutionStatus.CANCEL_REQUESTED]
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

    async def _retained_kernel_cleanup_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._cleanup_expired_retained_kernels()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Retained kernel cleanup failed")
            await asyncio.sleep(self._settings.execution_heartbeat_seconds)

    async def _dynamic_lifecycle_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._audit_dynamic_lifecycle()
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
        if current is not None and not current.done():
            if replace:
                current.cancel()
                task = asyncio.create_task(coroutine, name=f"cancel-{execution_id}")
                self._jobs[execution_id] = task
                task.add_done_callback(lambda done: self._remove_job_if_current(execution_id, done))
            else:
                coroutine.close()
            return
        task = asyncio.create_task(coroutine, name=f"execution-{execution_id}")
        self._jobs[execution_id] = task
        task.add_done_callback(lambda done: self._remove_job_if_current(execution_id, done))

    def _remove_job_if_current(self, execution_id: UUID, task: asyncio.Task[None]) -> None:
        if self._jobs.get(execution_id) is task:
            self._jobs.pop(execution_id, None)

    async def _run_execution(self, execution_id: UUID) -> None:
        with self._tracing.span(
            "executor.worker.execution",
            kind=SpanKind.CONSUMER,
            attributes={"executor.execution.id": str(execution_id)},
        ):
            await self._run_execution_impl(execution_id)

    async def _trace_jupyter[T](
        self,
        name: str,
        operation: Awaitable[T],
        *,
        execution_id: UUID,
        server_id: UUID,
        sequence: int | None = None,
    ) -> T:
        attributes: dict[str, object] = {
            "executor.execution.id": str(execution_id),
            "executor.jupyter.server.id": str(server_id),
        }
        if sequence is not None:
            attributes["executor.step.sequence"] = sequence
        with self._tracing.span(name, attributes=attributes):
            return await operation

    async def _run_execution_impl(self, execution_id: UUID) -> None:
        async with self._semaphore:
            claimed = await self._claim(execution_id)
            if claimed is None:
                return
            execution, server, attempt_id = claimed
            if execution.mode == ExecutionMode.DYNAMIC:
                await self._run_dynamic_execution(execution, server, attempt_id)
                return
            gateway = JupyterGateway(
                server.endpoint,
                self._registry.resolve_token(
                    server.credential_ref, server.credential_ciphertext
                ),
                self._settings.jupyter_request_timeout_seconds,
            )
            kernel_id: str | None = None
            heartbeat: asyncio.Task[None] | None = None
            failed_sequence: int | None = None
            try:
                workspace = self._workspace.prepare(execution)
                cells = self._workspace.load_cells(execution, workspace)
                await self._ensure_steps(execution.id, len(cells))
                resume = (
                    execution.retry_count > 0
                    and execution.retry_strategy == RetryStrategy.FROM_FAILED_STEP
                    and execution.retry_from_sequence is not None
                    and execution.kernel_id is not None
                )
                start_sequence = execution.retry_from_sequence if resume else 0
                if resume:
                    kernel_id = execution.kernel_id
                else:
                    kernel_id = await self._trace_jupyter(
                        "executor.jupyter.kernel.start",
                        gateway.start_kernel(
                            execution.kernel_name, workspace.jupyter_relative_path
                        ),
                        execution_id=execution.id,
                        server_id=server.id,
                    )
                await self._record_kernel(
                    execution.id,
                    attempt_id,
                    kernel_id,
                    workspace.jupyter_relative_path,
                    f"{workspace.jupyter_relative_path}/notebooks/execution.ipynb",
                )
                heartbeat = asyncio.create_task(
                    self._heartbeat(execution.id, attempt_id),
                    name=f"heartbeat-{execution.id}",
                )
                all_outputs: list[list[dict[str, object]]] = [
                    step.outputs
                    for step in execution.steps
                    if step.sequence < start_sequence
                ]
                execution_counts: list[int | None] = [None] * len(all_outputs)
                for sequence in range(start_sequence, len(cells)):
                    code = cells[sequence]
                    artifact_snapshot = self._artifacts.snapshot(workspace)
                    await self._step_started(execution.id, attempt_id, sequence)
                    try:
                        result = await self._trace_jupyter(
                            "executor.jupyter.cell.execute",
                            gateway.execute_cell(kernel_id, code),
                            execution_id=execution.id,
                            server_id=server.id,
                            sequence=sequence,
                        )
                    except JupyterExecutionError as exc:
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
                        self._workspace.write_notebook(
                            workspace, cells[: sequence + 1], all_outputs, execution_counts
                        )
                        try:
                            await self._artifacts.discover_and_register(
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
                        execution.id, attempt_id, sequence, result.outputs
                    )
                    self._workspace.write_notebook(
                        workspace, cells[: sequence + 1], all_outputs, execution_counts
                    )
                    try:
                        await self._artifacts.discover_and_register(
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
                    workspace=workspace,
                    execution_id=execution.id,
                    attempt_id=attempt_id,
                    sequence=len(cells) - 1,
                )
                await self._trace_jupyter(
                    "executor.jupyter.kernel.delete",
                    gateway.delete_kernel(kernel_id),
                    execution_id=execution.id,
                    server_id=server.id,
                )
                await self._finalize(
                    execution.id,
                    attempt_id,
                    ExecutionStatus.SUCCEEDED,
                    kernel_cleanup_status=KernelCleanupStatus.SUCCEEDED,
                )
            except asyncio.CancelledError:
                cleanup_status = KernelCleanupStatus.NOT_REQUIRED
                if kernel_id is not None:
                    try:
                        async with asyncio.timeout(
                            self._settings.execution_shutdown_cleanup_seconds
                        ):
                            cleanup_status = await _best_effort_kernel_stop(
                                gateway, kernel_id
                            )
                    except TimeoutError:
                        cleanup_status = KernelCleanupStatus.FAILED
                        logger.warning(
                            "Jupyter cleanup exceeded the Worker shutdown deadline",
                            extra={"execution_id": str(execution.id)},
                        )
                await self._finalize(
                    execution.id,
                    attempt_id,
                    ExecutionStatus.FAILED,
                    "Executor worker stopped while the execution was running.",
                    failure_type=FailureType.WORKER_SHUTDOWN,
                    retry_strategy=RetryStrategy.FROM_START,
                    kernel_cleanup_status=cleanup_status,
                )
                raise
            except Exception as exc:
                retain_kernel = (
                    isinstance(exc, JupyterExecutionError)
                    and kernel_id is not None
                    and failed_sequence is not None
                )
                failure_type, retry_strategy = _failure_policy(exc, retain_kernel)
                cleanup_status = KernelCleanupStatus.NOT_REQUIRED
                if kernel_id is not None and not retain_kernel:
                    cleanup_status = await _best_effort_kernel_stop(gateway, kernel_id)
                await self._finalize(
                    execution.id,
                    attempt_id,
                    ExecutionStatus.FAILED,
                    _safe_error(exc),
                    retain_kernel=retain_kernel,
                    retry_from_sequence=failed_sequence,
                    failure_type=failure_type,
                    retry_strategy=retry_strategy,
                    kernel_cleanup_status=cleanup_status,
                )
            finally:
                if heartbeat is not None:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
                await gateway.close()

    async def _claim(self, execution_id: UUID) -> tuple[Any, JupyterServerORM, UUID] | None:
        now = utc_now()
        lease_expires = now + timedelta(seconds=self._settings.execution_lease_seconds)
        async with self._session_factory() as session, session.begin():
            execution_row = await session.scalar(
                select(ExecutionORM)
                .where(ExecutionORM.id == execution_id)
                .options(selectinload(ExecutionORM.steps))
                .with_for_update()
            )
            if execution_row is None or execution_row.status != ExecutionStatus.QUEUED:
                return None
            if (
                execution_row.mode == ExecutionMode.DYNAMIC
                and execution_row.kernel_id is not None
                and execution_row.jupyter_server_id is not None
            ):
                waiting_attempt = await session.scalar(
                    select(ExecutionAttemptORM)
                    .where(
                        ExecutionAttemptORM.execution_id == execution_id,
                        ExecutionAttemptORM.status == AttemptStatus.WAITING,
                    )
                    .with_for_update()
                )
                server = await session.scalar(
                    select(JupyterServerORM)
                    .where(JupyterServerORM.id == execution_row.jupyter_server_id)
                    .with_for_update()
                )
                if (
                    waiting_attempt is None
                    or server is None
                    or not server.enabled
                    or server.status == JupyterServerStatus.OFFLINE
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
                execution_row.execution_expires_at = (
                    execution_row.execution_expires_at
                    or (execution_row.started_at or now)
                    + timedelta(seconds=self._settings.execution_max_runtime_seconds)
                )
                execution_row.updated_at = now
                execution_row.version += 1
                _add_outbox(
                    session,
                    execution_id,
                    "execution.resumed",
                    ExecutionStatus.RUNNING,
                )
                return execution_row.to_domain(), server, waiting_attempt.id
            is_resume = (
                execution_row.retry_count > 0
                and execution_row.retry_strategy == RetryStrategy.FROM_FAILED_STEP
                and execution_row.retry_from_sequence is not None
                and execution_row.kernel_id is not None
                and execution_row.jupyter_server_id is not None
            )
            if is_resume:
                server = await session.scalar(
                    select(JupyterServerORM)
                    .where(JupyterServerORM.id == execution_row.jupyter_server_id)
                    .with_for_update()
                )
                if (
                    server is None
                    or not server.enabled
                    or server.status == JupyterServerStatus.OFFLINE
                ):
                    is_resume = False
                    execution_row.retry_strategy = RetryStrategy.FROM_START
                    execution_row.retry_from_sequence = 0
                    execution_row.retained_kernel_until = None
                    execution_row.kernel_id = None
                    execution_row.jupyter_server_id = None
                    execution_row.kernel_cleanup_status = KernelCleanupStatus.FAILED
                    execution_row.updated_at = now
                    execution_row.version += 1
            if not is_resume:
                server = await self._select_server(session, execution_row)
            if server is None:
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
                    jupyter_server_id=server.id,
                    status=AttemptStatus.RUNNING,
                    lease_owner=self._consumer_name,
                    lease_expires_at=lease_expires,
                    heartbeat_at=now,
                    started_at=now,
                )
            )
            execution_row.status = ExecutionStatus.RUNNING
            execution_row.jupyter_server_id = server.id
            execution_row.lease_owner = self._consumer_name
            execution_row.lease_expires_at = lease_expires
            execution_row.heartbeat_at = now
            started_at = execution_row.started_at or now
            execution_row.started_at = started_at
            execution_row.execution_expires_at = (
                execution_row.execution_expires_at
                or started_at
                + timedelta(seconds=self._settings.execution_max_runtime_seconds)
            )
            execution_row.error_message = None
            execution_row.failure_type = None
            execution_row.retryable = False
            execution_row.retained_kernel_until = None
            execution_row.kernel_cleanup_status = KernelCleanupStatus.NOT_REQUIRED
            execution_row.updated_at = now
            execution_row.version += 1
            _add_outbox(session, execution_id, "execution.started", ExecutionStatus.RUNNING)
            return execution_row.to_domain(), server, attempt_id

    async def _select_server(
        self, session: AsyncSession, execution: ExecutionORM
    ) -> JupyterServerORM | None:
        servers = list(
            await session.scalars(
                select(JupyterServerORM)
                .where(
                    JupyterServerORM.pool == execution.jupyter_pool,
                    JupyterServerORM.enabled.is_(True),
                    JupyterServerORM.status == JupyterServerStatus.ACTIVE,
                )
                .order_by(JupyterServerORM.name)
                .with_for_update(skip_locked=True)
            )
        )
        for server in servers:
            if server.supported_kernels and execution.kernel_name not in server.supported_kernels:
                continue
            running = await session.scalar(
                select(func.count(ExecutionAttemptORM.id)).where(
                    ExecutionAttemptORM.jupyter_server_id == server.id,
                    ExecutionAttemptORM.status.in_(
                        [AttemptStatus.RUNNING, AttemptStatus.WAITING]
                    ),
                )
            )
            retained = await session.scalar(
                select(func.count(ExecutionORM.id)).where(
                    ExecutionORM.jupyter_server_id == server.id,
                    ExecutionORM.status == ExecutionStatus.FAILED,
                    ExecutionORM.retryable.is_(True),
                    ExecutionORM.retained_kernel_until > utc_now(),
                )
            )
            if (running or 0) + (retained or 0) < server.max_concurrent_executions:
                return server
        return None

    async def _run_dynamic_execution(
        self, execution: Execution, server: JupyterServerORM, attempt_id: UUID
    ) -> None:
        gateway = JupyterGateway(
            server.endpoint,
            self._registry.resolve_token(server.credential_ref, server.credential_ciphertext),
            self._settings.jupyter_request_timeout_seconds,
        )
        heartbeat: asyncio.Task[None] | None = None
        kernel_id = execution.kernel_id
        try:
            workspace = self._workspace.prepare(execution)
            if kernel_id is None:
                kernel_id = await self._trace_jupyter(
                    "executor.jupyter.kernel.start",
                    gateway.start_kernel(
                        execution.kernel_name, workspace.jupyter_relative_path
                    ),
                    execution_id=execution.id,
                    server_id=server.id,
                )
            await self._record_kernel(
                execution.id,
                attempt_id,
                kernel_id,
                workspace.jupyter_relative_path,
                f"{workspace.jupyter_relative_path}/notebooks/execution.ipynb",
            )
            heartbeat = asyncio.create_task(
                self._heartbeat(execution.id, attempt_id),
                name=f"heartbeat-{execution.id}",
            )
            if execution.dynamic_finish_requested:
                last_sequence = max((step.sequence for step in execution.steps), default=0)
                await self._artifacts.register_notebook(
                    workspace=workspace,
                    execution_id=execution.id,
                    attempt_id=attempt_id,
                    sequence=last_sequence,
                )
                await self._trace_jupyter(
                    "executor.jupyter.kernel.delete",
                    gateway.delete_kernel(kernel_id),
                    execution_id=execution.id,
                    server_id=server.id,
                )
                await self._finalize(
                    execution.id,
                    attempt_id,
                    ExecutionStatus.SUCCEEDED,
                    kernel_cleanup_status=KernelCleanupStatus.SUCCEEDED,
                )
                return

            pending = next(
                (step for step in execution.steps if step.status == StepStatus.PENDING), None
            )
            if pending is None or not pending.code:
                raise ValueError("Queued DYNAMIC execution has no pending cell code.")
            artifact_snapshot = self._artifacts.snapshot(workspace)
            await self._step_started(execution.id, attempt_id, pending.sequence)
            try:
                result = await self._trace_jupyter(
                    "executor.jupyter.cell.execute",
                    gateway.execute_cell(kernel_id, pending.code),
                    execution_id=execution.id,
                    server_id=server.id,
                    sequence=pending.sequence,
                )
            except JupyterExecutionError as exc:
                await self._step_failed(
                    execution.id,
                    attempt_id,
                    pending.sequence,
                    exc.outputs,
                    str(exc),
                )
                await self._write_dynamic_notebook(execution.id, workspace)
                try:
                    await self._artifacts.discover_and_register(
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
                await self._pause_dynamic(
                    execution.id, attempt_id, pending.sequence, StepStatus.FAILED
                )
                return
            await self._step_succeeded(
                execution.id, attempt_id, pending.sequence, result.outputs
            )
            await self._write_dynamic_notebook(execution.id, workspace)
            try:
                await self._artifacts.discover_and_register(
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
            await self._pause_dynamic(
                execution.id, attempt_id, pending.sequence, StepStatus.SUCCEEDED
            )
        except asyncio.CancelledError:
            cleanup_status = KernelCleanupStatus.NOT_REQUIRED
            if kernel_id is not None:
                cleanup_status = await _best_effort_kernel_stop(gateway, kernel_id)
            await self._finalize(
                execution.id,
                attempt_id,
                ExecutionStatus.FAILED,
                "Executor worker stopped while the dynamic cell was running.",
                failure_type=FailureType.WORKER_SHUTDOWN,
                retry_strategy=RetryStrategy.NOT_RETRYABLE,
                kernel_cleanup_status=cleanup_status,
            )
            raise
        except Exception as exc:
            cleanup_status = KernelCleanupStatus.NOT_REQUIRED
            if kernel_id is not None:
                cleanup_status = await _best_effort_kernel_stop(gateway, kernel_id)
            await self._finalize(
                execution.id,
                attempt_id,
                ExecutionStatus.FAILED,
                _safe_error(exc),
                failure_type=_failure_policy(exc, False)[0],
                retry_strategy=RetryStrategy.NOT_RETRYABLE,
                kernel_cleanup_status=cleanup_status,
            )
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            await gateway.close()

    async def _write_dynamic_notebook(
        self, execution_id: UUID, workspace: ExecutionWorkspace
    ) -> None:
        async with self._session_factory() as session:
            steps = list(
                await session.scalars(
                    select(ExecutionStepORM)
                    .where(ExecutionStepORM.execution_id == execution_id)
                    .order_by(ExecutionStepORM.sequence)
                )
            )
        cells = [step.code or "" for step in steps if step.status != StepStatus.PENDING]
        outputs = [step.outputs for step in steps if step.status != StepStatus.PENDING]
        self._workspace.write_notebook(workspace, cells, outputs, [None] * len(cells))

    async def _pause_dynamic(
        self,
        execution_id: UUID,
        attempt_id: UUID,
        sequence: int,
        step_status: StepStatus,
    ) -> None:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            execution = await session.scalar(
                select(ExecutionORM)
                .where(ExecutionORM.id == execution_id)
                .with_for_update()
            )
            if execution is None or execution.status != ExecutionStatus.RUNNING:
                return
            execution.status = ExecutionStatus.WAITING_FOR_NEXT_STEP
            execution.lease_owner = None
            execution.lease_expires_at = None
            execution.updated_at = now
            execution.dynamic_finish_requested = False
            wait_deadline = now + timedelta(
                seconds=self._settings.dynamic_step_wait_timeout_seconds
            )
            execution.dynamic_wait_expires_at = min(
                wait_deadline,
                execution.execution_expires_at or wait_deadline,
            )
            execution.version += 1
            await session.execute(
                update(ExecutionAttemptORM)
                .where(ExecutionAttemptORM.id == attempt_id)
                .values(
                    status=AttemptStatus.WAITING,
                    lease_owner=None,
                    lease_expires_at=None,
                )
            )
            _add_outbox(
                session,
                execution_id,
                (
                    "execution.step_completed"
                    if step_status == StepStatus.SUCCEEDED
                    else "execution.step_failed"
                ),
                ExecutionStatus.WAITING_FOR_NEXT_STEP,
                {
                    "execution_attempt_id": str(attempt_id),
                    "sequence": sequence,
                    "step_status": step_status.value,
                    "version": execution.version,
                },
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

    async def _record_kernel(
        self,
        execution_id: UUID,
        attempt_id: UUID,
        kernel_id: str,
        workspace_path: str,
        notebook_path: str,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(ExecutionORM)
                .where(ExecutionORM.id == execution_id)
                .values(
                    kernel_id=kernel_id,
                    workspace_path=workspace_path,
                    notebook_path=notebook_path,
                    updated_at=utc_now(),
                )
            )
            await session.execute(
                update(ExecutionAttemptORM)
                .where(ExecutionAttemptORM.id == attempt_id)
                .values(kernel_id=kernel_id)
            )

    async def _step_started(
        self, execution_id: UUID, attempt_id: UUID, sequence: int
    ) -> None:
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
                        started_at=now,
                    )
                )
            else:
                history.status = StepStatus.RUNNING
                history.started_at = now
                history.finished_at = None
                history.error_message = None
                history.outputs = []

    async def _step_succeeded(
        self,
        execution_id: UUID,
        attempt_id: UUID,
        sequence: int,
        outputs: list[dict[str, Any]],
    ) -> None:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(ExecutionStepORM)
                .where(
                    ExecutionStepORM.execution_id == execution_id,
                    ExecutionStepORM.sequence == sequence,
                )
                .values(
                    status=StepStatus.SUCCEEDED,
                    outputs=outputs,
                    finished_at=now,
                    updated_at=now,
                )
            )
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
            await session.execute(
                update(ExecutionStepORM)
                .where(
                    ExecutionStepORM.execution_id == execution_id,
                    ExecutionStepORM.sequence == sequence,
                )
                .values(
                    status=StepStatus.FAILED,
                    outputs=outputs,
                    error_message=error_message[:2000],
                    finished_at=now,
                    updated_at=now,
                )
            )
            await session.execute(
                update(ExecutionStepAttemptORM)
                .where(
                    ExecutionStepAttemptORM.execution_attempt_id == attempt_id,
                    ExecutionStepAttemptORM.sequence == sequence,
                )
                .values(
                    status=StepStatus.FAILED,
                    outputs=outputs,
                    error_message=error_message[:2000],
                    finished_at=now,
                )
            )

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
                carrier = capture_trace_carrier()
                event = OutboxEvent(
                    aggregate_type="Execution",
                    aggregate_id=execution_id,
                    event_type="execution.artifact_failed",
                    payload={
                        "execution_id": str(execution_id),
                        "execution_attempt_id": str(attempt_id),
                        "sequence": sequence,
                        "error_type": type(error).__name__,
                    },
                    traceparent=carrier.traceparent,
                    tracestate=carrier.tracestate,
                )
                session.add(OutboxEventORM.from_domain(event))
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
        retain_kernel: bool = False,
        retry_from_sequence: int | None = None,
        failure_type: FailureType | None = None,
        retry_strategy: RetryStrategy = RetryStrategy.NOT_RETRYABLE,
        kernel_cleanup_status: KernelCleanupStatus = KernelCleanupStatus.NOT_REQUIRED,
    ) -> None:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            execution = await session.scalar(
                select(ExecutionORM).where(ExecutionORM.id == execution_id).with_for_update()
            )
            if execution is None or execution.status.is_terminal:
                return
            status = (
                ExecutionStatus.CANCELLED
                if execution.status == ExecutionStatus.CANCEL_REQUESTED
                else requested_status
            )
            attempt_status = AttemptStatus(status.value)
            is_failed = status == ExecutionStatus.FAILED
            effective_failure_type = failure_type if is_failed else None
            effective_retry_strategy = (
                retry_strategy if is_failed else RetryStrategy.NOT_RETRYABLE
            )
            execution.status = status
            execution.error_message = error_message if is_failed else None
            execution.failure_type = effective_failure_type
            execution.finished_at = now
            execution.updated_at = now
            execution.lease_owner = None
            execution.lease_expires_at = None
            execution.dynamic_wait_expires_at = None
            execution.retry_strategy = effective_retry_strategy
            execution.retryable = effective_retry_strategy != RetryStrategy.NOT_RETRYABLE
            execution.retry_from_sequence = None
            if effective_retry_strategy == RetryStrategy.FROM_FAILED_STEP:
                execution.retry_from_sequence = retry_from_sequence
            elif effective_retry_strategy == RetryStrategy.FROM_START:
                execution.retry_from_sequence = 0
            execution.retained_kernel_until = (
                now
                + timedelta(seconds=self._settings.failed_kernel_retention_seconds)
                if is_failed and retain_kernel
                else None
            )
            execution.kernel_cleanup_status = kernel_cleanup_status
            if (
                not retain_kernel
                and kernel_cleanup_status == KernelCleanupStatus.SUCCEEDED
            ):
                execution.kernel_id = None
            execution.version += 1
            await session.execute(
                update(ExecutionAttemptORM)
                .where(ExecutionAttemptORM.id == attempt_id)
                .values(
                    status=attempt_status,
                    error_message=error_message if is_failed else None,
                    failure_type=effective_failure_type,
                    retry_strategy=effective_retry_strategy,
                    kernel_cleanup_status=kernel_cleanup_status,
                    finished_at=now,
                )
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
            _add_outbox(
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
                    "kernel_cleanup_status": kernel_cleanup_status.value,
                },
            )

    async def _cancel_execution(self, execution_id: UUID) -> None:
        server: JupyterServerORM | None = None
        kernel_id: str | None = None
        cleanup_status = KernelCleanupStatus.NOT_REQUIRED
        async with self._session_factory() as session:
            execution = await session.get(ExecutionORM, execution_id)
            if execution is None or execution.status != ExecutionStatus.CANCEL_REQUESTED:
                return
            kernel_id = execution.kernel_id
            if execution.jupyter_server_id is not None:
                server = await session.get(JupyterServerORM, execution.jupyter_server_id)
        if server is not None and kernel_id is not None:
            gateway = JupyterGateway(
                server.endpoint,
                self._registry.resolve_token(
                    server.credential_ref, server.credential_ciphertext
                ),
                self._settings.jupyter_request_timeout_seconds,
            )
            try:
                cleanup_status = await _best_effort_kernel_stop(gateway, kernel_id)
            finally:
                await gateway.close()
        elif kernel_id is not None:
            cleanup_status = KernelCleanupStatus.FAILED
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
            execution.dynamic_wait_expires_at = None
            execution.failure_type = None
            execution.retryable = False
            execution.retry_strategy = RetryStrategy.NOT_RETRYABLE
            execution.retry_from_sequence = None
            execution.retained_kernel_until = None
            execution.kernel_cleanup_status = cleanup_status
            if cleanup_status == KernelCleanupStatus.SUCCEEDED:
                execution.kernel_id = None
            execution.version += 1
            await session.execute(
                update(ExecutionAttemptORM)
                .where(
                    ExecutionAttemptORM.execution_id == execution_id,
                    ExecutionAttemptORM.status.in_(
                        [AttemptStatus.RUNNING, AttemptStatus.WAITING]
                    ),
                )
                .values(
                    status=AttemptStatus.CANCELLED,
                    failure_type=None,
                    retry_strategy=RetryStrategy.NOT_RETRYABLE,
                    kernel_cleanup_status=cleanup_status,
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
                update(ExecutionStepAttemptORM)
                .where(
                    ExecutionStepAttemptORM.execution_id == execution_id,
                    ExecutionStepAttemptORM.status == StepStatus.RUNNING,
                )
                .values(status=StepStatus.CANCELLED, finished_at=now)
            )
            _add_outbox(
                session,
                execution_id,
                "execution.cancelled",
                ExecutionStatus.CANCELLED,
                {"kernel_cleanup_status": cleanup_status.value},
            )

    async def _audit_dynamic_lifecycle(self) -> None:
        await self._request_expired_execution_cancellations()
        now = utc_now()
        async with self._session_factory() as session:
            waiting = list(
                await session.execute(
                    select(ExecutionORM, JupyterServerORM)
                    .join(
                        JupyterServerORM,
                        JupyterServerORM.id == ExecutionORM.jupyter_server_id,
                    )
                    .where(
                        ExecutionORM.mode == ExecutionMode.DYNAMIC,
                        ExecutionORM.status == ExecutionStatus.WAITING_FOR_NEXT_STEP,
                    )
                    .order_by(ExecutionORM.updated_at)
                    .limit(200)
                )
            )
        for execution, server in waiting:
            if (
                execution.execution_expires_at is not None
                and _as_utc(execution.execution_expires_at) <= now
            ):
                await self._fail_waiting_execution(
                    execution.id,
                    execution.kernel_id,
                    FailureType.EXECUTION_TIMEOUT,
                    "Execution exceeded its maximum runtime while waiting for the Agent.",
                )
                continue
            if (
                execution.dynamic_wait_expires_at is not None
                and _as_utc(execution.dynamic_wait_expires_at) <= now
            ):
                await self._fail_waiting_execution(
                    execution.id,
                    execution.kernel_id,
                    FailureType.DYNAMIC_WAIT_TIMEOUT,
                    "Agent did not provide the next dynamic step before the deadline.",
                )
                continue
            if not server.enabled:
                await self._fail_waiting_execution(
                    execution.id,
                    execution.kernel_id,
                    FailureType.JUPYTER_UNAVAILABLE,
                    "The assigned Jupyter server was removed while waiting for the Agent.",
                )
                continue
            if execution.kernel_id is None:
                await self._fail_waiting_execution(
                    execution.id,
                    None,
                    FailureType.KERNEL_LOST,
                    "The retained dynamic Jupyter kernel reference was lost.",
                )
                continue
            gateway = JupyterGateway(
                server.endpoint,
                self._registry.resolve_token(
                    server.credential_ref, server.credential_ciphertext
                ),
                self._settings.jupyter_request_timeout_seconds,
            )
            try:
                kernel_exists = await gateway.kernel_exists(execution.kernel_id)
            except JupyterGatewayError:
                # OFFLINE can be temporary. The persisted deadlines remain the terminal guard.
                continue
            finally:
                await gateway.close()
            if not kernel_exists:
                await self._fail_waiting_execution(
                    execution.id,
                    execution.kernel_id,
                    FailureType.KERNEL_LOST,
                    "The retained dynamic Jupyter kernel no longer exists.",
                )

    async def _request_expired_execution_cancellations(self) -> None:
        now = utc_now()
        expired_ids: list[UUID] = []
        async with self._session_factory() as session, session.begin():
            expired = list(
                await session.scalars(
                    select(ExecutionORM)
                    .where(
                        ExecutionORM.status.in_(
                            [ExecutionStatus.QUEUED, ExecutionStatus.RUNNING]
                        ),
                        ExecutionORM.execution_expires_at.is_not(None),
                        ExecutionORM.execution_expires_at <= now,
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            for execution in expired:
                execution.status = ExecutionStatus.CANCEL_REQUESTED
                execution.cancellation_reason = "Execution exceeded its maximum runtime."
                execution.dynamic_wait_expires_at = None
                execution.updated_at = now
                execution.version += 1
                expired_ids.append(execution.id)
                _add_outbox(
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
        expected_kernel_id: str | None,
        failure_type: FailureType,
        error_message: str,
    ) -> None:
        now = utc_now()
        cleanup_target: tuple[UUID, UUID | None, UUID, str] | None = None
        async with self._session_factory() as session, session.begin():
            execution = await session.scalar(
                select(ExecutionORM)
                .where(ExecutionORM.id == execution_id)
                .with_for_update()
            )
            if (
                execution is None
                or execution.status != ExecutionStatus.WAITING_FOR_NEXT_STEP
                or execution.kernel_id != expected_kernel_id
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
                failure_type != FailureType.KERNEL_LOST
                and execution.kernel_id is not None
                and execution.jupyter_server_id is not None
            )
            cleanup_status = (
                KernelCleanupStatus.PENDING
                if cleanup_required
                else KernelCleanupStatus.NOT_REQUIRED
            )
            execution.status = ExecutionStatus.FAILED
            execution.error_message = error_message
            execution.failure_type = failure_type
            execution.finished_at = now
            execution.updated_at = now
            execution.lease_owner = None
            execution.lease_expires_at = None
            execution.dynamic_wait_expires_at = None
            execution.retryable = False
            execution.retry_strategy = RetryStrategy.NOT_RETRYABLE
            execution.retry_from_sequence = None
            execution.retained_kernel_until = None
            execution.kernel_cleanup_status = cleanup_status
            execution.recovery_count += 1
            execution.version += 1
            if failure_type == FailureType.KERNEL_LOST:
                execution.kernel_id = None
            if attempt is not None:
                attempt.status = AttemptStatus.FAILED
                attempt.lease_owner = None
                attempt.lease_expires_at = None
                attempt.error_message = error_message
                attempt.failure_type = failure_type
                attempt.retry_strategy = RetryStrategy.NOT_RETRYABLE
                attempt.kernel_cleanup_status = cleanup_status
                attempt.finished_at = now
            if cleanup_required:
                if execution.jupyter_server_id is None or expected_kernel_id is None:
                    raise RuntimeError("Dynamic cleanup target unexpectedly missing.")
                cleanup_target = (
                    execution.id,
                    attempt.id if attempt is not None else None,
                    execution.jupyter_server_id,
                    expected_kernel_id,
                )
            _add_outbox(
                session,
                execution.id,
                "execution.failed",
                ExecutionStatus.FAILED,
                {
                    "failure_type": failure_type.value,
                    "retry_strategy": RetryStrategy.NOT_RETRYABLE.value,
                    "kernel_cleanup_status": cleanup_status.value,
                    "recovery_count": execution.recovery_count,
                },
            )
        if cleanup_target is not None:
            await self._cleanup_abandoned_kernel(*cleanup_target)

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
                    if execution.mode == ExecutionMode.DYNAMIC
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
                    execution.jupyter_server_id is not None
                    and execution.kernel_id is not None
                    and attempt is not None
                ):
                    cleanup_targets.append(
                        (
                            execution.id,
                            attempt.id,
                            execution.jupyter_server_id,
                            execution.kernel_id,
                        )
                    )
                execution.status = ExecutionStatus.FAILED
                execution.error_message = "Worker lease expired; execution requires retry."
                execution.failure_type = FailureType.LEASE_EXPIRED
                execution.finished_at = now
                execution.updated_at = now
                execution.lease_owner = None
                execution.lease_expires_at = None
                execution.retryable = retry_strategy != RetryStrategy.NOT_RETRYABLE
                execution.retry_strategy = retry_strategy
                execution.retry_from_sequence = (
                    0 if retry_strategy == RetryStrategy.FROM_START else None
                )
                execution.retained_kernel_until = None
                execution.recovery_count += 1
                execution.kernel_cleanup_status = (
                    KernelCleanupStatus.PENDING
                    if cleanup_targets and cleanup_targets[-1][0] == execution.id
                    else KernelCleanupStatus.NOT_REQUIRED
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
                        kernel_cleanup_status=execution.kernel_cleanup_status,
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
                _add_outbox(
                    session,
                    execution.id,
                    "execution.failed",
                    ExecutionStatus.FAILED,
                    {
                        "failure_type": FailureType.LEASE_EXPIRED.value,
                        "retry_strategy": retry_strategy.value,
                        "retry_from_sequence": execution.retry_from_sequence,
                        "kernel_cleanup_status": execution.kernel_cleanup_status.value,
                        "recovery_count": execution.recovery_count,
                    },
                )
        for execution_id, attempt_id, server_id, kernel_id in cleanup_targets:
            await self._cleanup_abandoned_kernel(
                execution_id, attempt_id, server_id, kernel_id
            )

    async def _cleanup_abandoned_kernel(
        self,
        execution_id: UUID,
        attempt_id: UUID | None,
        server_id: UUID,
        kernel_id: str,
    ) -> None:
        async with self._session_factory() as session:
            server = await session.get(JupyterServerORM, server_id)
        if server is None:
            await self._record_cleanup_result(
                execution_id, attempt_id, kernel_id, KernelCleanupStatus.FAILED
            )
            return
        gateway = JupyterGateway(
            server.endpoint,
            self._registry.resolve_token(
                server.credential_ref, server.credential_ciphertext
            ),
            self._settings.jupyter_request_timeout_seconds,
        )
        try:
            await gateway.delete_kernel(kernel_id)
        except Exception:
            logger.warning(
                "Abandoned kernel cleanup failed",
                extra={"execution_id": str(execution_id)},
            )
            cleanup_status = KernelCleanupStatus.FAILED
        else:
            cleanup_status = KernelCleanupStatus.SUCCEEDED
        finally:
            await gateway.close()
        await self._record_cleanup_result(
            execution_id, attempt_id, kernel_id, cleanup_status
        )

    async def _record_cleanup_result(
        self,
        execution_id: UUID,
        attempt_id: UUID | None,
        kernel_id: str,
        cleanup_status: KernelCleanupStatus,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            execution_update = await session.execute(
                update(ExecutionORM)
                .where(
                    ExecutionORM.id == execution_id,
                    ExecutionORM.status == ExecutionStatus.FAILED,
                    ExecutionORM.kernel_id == kernel_id,
                )
                .values(
                    kernel_id=(
                        None
                        if cleanup_status == KernelCleanupStatus.SUCCEEDED
                        else kernel_id
                    ),
                    kernel_cleanup_status=cleanup_status,
                    updated_at=utc_now(),
                    version=ExecutionORM.version + 1,
                )
            )
            if attempt_id is not None:
                await session.execute(
                    update(ExecutionAttemptORM)
                    .where(ExecutionAttemptORM.id == attempt_id)
                    .values(kernel_cleanup_status=cleanup_status)
                )
            if getattr(execution_update, "rowcount", None) == 1:
                _add_outbox(
                    session,
                    execution_id,
                    (
                        "execution.kernel_cleanup_completed"
                        if cleanup_status == KernelCleanupStatus.SUCCEEDED
                        else "execution.kernel_cleanup_failed"
                    ),
                    ExecutionStatus.FAILED,
                    {"kernel_cleanup_status": cleanup_status.value},
                )

    async def _cleanup_expired_retained_kernels(self) -> None:
        now = utc_now()
        async with self._session_factory() as session:
            rows = list(
                await session.execute(
                    select(ExecutionORM, JupyterServerORM)
                    .join(
                        JupyterServerORM,
                        JupyterServerORM.id == ExecutionORM.jupyter_server_id,
                    )
                    .where(
                        ExecutionORM.status == ExecutionStatus.FAILED,
                        ExecutionORM.retryable.is_(True),
                        ExecutionORM.retained_kernel_until <= now,
                        ExecutionORM.kernel_id.is_not(None),
                    )
                )
            )
        for execution, server in rows:
            gateway = JupyterGateway(
                server.endpoint,
                self._registry.resolve_token(
                    server.credential_ref, server.credential_ciphertext
                ),
                self._settings.jupyter_request_timeout_seconds,
            )
            cleanup_status = KernelCleanupStatus.SUCCEEDED
            try:
                if execution.kernel_id is not None:
                    await gateway.delete_kernel(execution.kernel_id)
            except Exception:
                cleanup_status = KernelCleanupStatus.FAILED
                logger.warning(
                    "Expired retained kernel cleanup failed",
                    extra={"execution_id": str(execution.id)},
                )
            finally:
                await gateway.close()
            async with self._session_factory() as update_session, update_session.begin():
                current = await update_session.scalar(
                    select(ExecutionORM)
                    .where(ExecutionORM.id == execution.id)
                    .with_for_update()
                )
                if (
                    current is None
                    or current.status != ExecutionStatus.FAILED
                    or not current.retryable
                    or current.retained_kernel_until is None
                    or current.retained_kernel_until > now
                ):
                    continue
                current.retryable = False
                current.retry_strategy = RetryStrategy.NOT_RETRYABLE
                current.retry_from_sequence = None
                current.retained_kernel_until = None
                if cleanup_status == KernelCleanupStatus.SUCCEEDED:
                    current.kernel_id = None
                current.kernel_cleanup_status = cleanup_status
                current.updated_at = now
                current.version += 1
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
                        .values(kernel_cleanup_status=cleanup_status)
                    )
                _add_outbox(
                    update_session,
                    current.id,
                    "execution.retry_window_expired",
                    ExecutionStatus.FAILED,
                    {"kernel_cleanup_status": cleanup_status.value},
                )


def _add_outbox(
    session: AsyncSession,
    execution_id: UUID,
    event_type: str,
    status: ExecutionStatus,
    details: dict[str, object] | None = None,
) -> None:
    payload: dict[str, object] = {
        "execution_id": str(execution_id),
        "status": status.value,
    }
    if details:
        payload.update(details)
    carrier = capture_trace_carrier()
    event = OutboxEvent(
        aggregate_type="Execution",
        aggregate_id=execution_id,
        event_type=event_type,
        payload=payload,
        traceparent=carrier.traceparent,
        tracestate=carrier.tracestate,
    )
    session.add(OutboxEventORM.from_domain(event))


def _failure_policy(
    exc: Exception, retain_kernel: bool
) -> tuple[FailureType, RetryStrategy]:
    if isinstance(exc, JupyterExecutionError) and retain_kernel:
        return FailureType.TOOL_ERROR, RetryStrategy.FROM_FAILED_STEP
    if isinstance(exc, JupyterGatewayError):
        return FailureType.JUPYTER_UNAVAILABLE, RetryStrategy.FROM_START
    return FailureType.INTERNAL_ERROR, RetryStrategy.NOT_RETRYABLE


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, (JupyterExecutionError, JupyterGatewayError)):
        return str(exc)[:2000]
    return f"{type(exc).__name__}: execution failed"[:2000]


async def _best_effort_kernel_stop(
    gateway: JupyterGateway, kernel_id: str
) -> KernelCleanupStatus:
    try:
        await gateway.interrupt_kernel(kernel_id)
        await gateway.delete_kernel(kernel_id)
    except Exception:
        logger.warning("Jupyter kernel cleanup failed", extra={"kernel_id": kernel_id})
        return KernelCleanupStatus.FAILED
    return KernelCleanupStatus.SUCCEEDED


def _as_utc(value: datetime) -> datetime:
    """SQLite tests may return timezone-naive values for aware columns."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
