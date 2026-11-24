"""Read a Runtime Target's kernels, tie them to Executions, delete unheld ones."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.application.execution_queries import (
    ExecutionDetailView,
)
from executor_service.application.kernel_usage import (
    ExecutionKernelUsageView,
    KernelState,
    RecordedStepUsageView,
    RuntimeTargetKernelsView,
    TargetKernelView,
)
from executor_service.domain.errors import (
    RuntimeKernelDeleteConflictError,
    RuntimeUnavailableError,
)
from executor_service.domain.models import utc_now
from executor_service.domain.runtime import (
    RuntimeDriver,
    RuntimeDriverFactory,
    RuntimeKernelUsage,
)
from executor_service.infrastructure.db.models import (
    ExecutionORM,
    ExecutionStepAttemptORM,
)
from executor_service.infrastructure.runtime_registry import (
    RuntimeTargetRegistry,
)
from executor_service.infrastructure.runtime_registry.targets import (
    required_target,
)

logger = logging.getLogger(__name__)

# A Worker records a kernel right after creating it, within the database
# timeouts (about two minutes at most). A younger unheld kernel may be one
# about to be recorded, so it is never deleted.
UNHELD_KERNEL_MIN_AGE_SECONDS = 900


class FleetRuntimeKernels:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        registry: RuntimeTargetRegistry,
        driver_factory: RuntimeDriverFactory,
    ) -> None:
        self._session_factory = session_factory
        self._registry = registry
        self._driver_factory = driver_factory

    async def execution_usage(
        self, execution: ExecutionDetailView
    ) -> ExecutionKernelUsageView:
        async with self._session_factory() as session:
            peak, cpu, measured = (
                await session.execute(
                    select(
                        func.max(ExecutionStepAttemptORM.peak_memory_bytes),
                        func.sum(ExecutionStepAttemptORM.cpu_seconds),
                        func.count(ExecutionStepAttemptORM.peak_memory_bytes),
                    ).where(
                        ExecutionStepAttemptORM.execution_id == execution.id
                    )
                )
            ).one()
        kernel_id = execution.runtime_session_id
        target_id = execution.runtime_target_id
        kernel: RuntimeKernelUsage | None = None
        observed_at = None
        state: KernelState = "NO_KERNEL"
        if kernel_id is not None and target_id is not None:
            try:
                kernels = await self._observe(target_id)
            except RuntimeUnavailableError:
                state = "RUNTIME_UNAVAILABLE"
            else:
                observed_at = utc_now()
                kernel = next(
                    (item for item in kernels if item.kernel_id == kernel_id),
                    None,
                )
                state = "RUNNING" if kernel is not None else "NOT_RUNNING"
        return ExecutionKernelUsageView(
            execution_id=execution.id,
            target_id=target_id,
            kernel_id=kernel_id,
            kernel_state=state,
            kernel=kernel,
            observed_at=observed_at,
            steps=RecordedStepUsageView(
                peak_memory_bytes=peak,
                cpu_seconds=round(cpu, 3) if cpu is not None else None,
                measured_step_attempt_count=measured,
            ),
        )

    async def target_kernels(
        self, target_id: UUID
    ) -> RuntimeTargetKernelsView:
        kernels = await self._observe(target_id)
        observed_at = utc_now()
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        ExecutionORM.runtime_session_id,
                        ExecutionORM.id,
                        ExecutionORM.tenant_id,
                        ExecutionORM.status,
                    ).where(
                        ExecutionORM.runtime_target_id == target_id,
                        ExecutionORM.runtime_session_id.in_(
                            [kernel.kernel_id for kernel in kernels]
                        ),
                    )
                )
            ).all()
        holders = {row.runtime_session_id: row for row in rows}

        def view(kernel: RuntimeKernelUsage) -> TargetKernelView:
            holder = holders.get(kernel.kernel_id)
            if holder is None:
                return TargetKernelView(kernel, None, None, None)
            return TargetKernelView(
                kernel, holder.id, holder.tenant_id, holder.status
            )

        return RuntimeTargetKernelsView(
            target_id=target_id,
            observed_at=observed_at,
            kernels=tuple(view(kernel) for kernel in kernels),
        )

    async def delete_unheld_kernel(
        self, target_id: UUID, kernel_id: str
    ) -> bool:
        # List the kernels before reading holders: a kernel recorded after
        # the listing is then seen as held, never as left behind.
        async with self._driver(target_id) as driver:
            kernel = next(
                (
                    item
                    for item in await self._read_kernels(driver, target_id)
                    if item.kernel_id == kernel_id
                ),
                None,
            )
            if kernel is None:
                return False
            async with self._session_factory() as session:
                # Any Execution, in any status and on any Target: a retained
                # or cleanup-pending kernel is still its Execution's.
                holder = await session.scalar(
                    select(ExecutionORM.id)
                    .where(ExecutionORM.runtime_session_id == kernel_id)
                    .limit(1)
                )
            location = ("path", "kernel_id")
            if holder is not None:
                raise RuntimeKernelDeleteConflictError.at(
                    location,
                    "An Execution holds this kernel; cancel the Execution.",
                    "kernel_held",
                    execution_id=str(holder),
                )
            if (
                kernel.age_seconds is None
                or kernel.age_seconds < UNHELD_KERNEL_MIN_AGE_SECONDS
            ):
                raise RuntimeKernelDeleteConflictError.at(
                    location,
                    "The kernel may be about to be recorded by a Worker.",
                    "kernel_too_recent",
                    age_seconds=kernel.age_seconds,
                    min_age_seconds=UNHELD_KERNEL_MIN_AGE_SECONDS,
                )
            try:
                await driver.delete_session(kernel_id)
            except Exception as exc:
                raise RuntimeUnavailableError(
                    "The Runtime Target did not delete the kernel."
                ) from exc
        logger.info(
            "Deleted unheld Runtime kernel runtime_target_id=%s kernel_id=%s "
            "age_seconds=%s memory_bytes=%s",
            target_id,
            kernel_id,
            kernel.age_seconds,
            kernel.memory_bytes,
        )
        return True

    async def _observe(
        self, target_id: UUID
    ) -> tuple[RuntimeKernelUsage, ...]:
        async with self._driver(target_id) as driver:
            return await self._read_kernels(driver, target_id)

    @asynccontextmanager
    async def _driver(self, target_id: UUID) -> AsyncIterator[RuntimeDriver]:
        async with self._session_factory() as session:
            target = await required_target(session, target_id)
        driver = self._driver_factory.create(
            target.runtime_type,
            target.connection_config,
            self._registry.resolve_credential(
                target.credential_ref, target.credential_ciphertext
            ),
        )
        try:
            yield driver
        finally:
            await driver.close()

    @staticmethod
    async def _read_kernels(
        driver: RuntimeDriver, target_id: UUID
    ) -> tuple[RuntimeKernelUsage, ...]:
        try:
            return await driver.kernel_usage()
        except Exception as exc:
            logger.warning(
                "Runtime kernel usage read failed runtime_target_id=%s "
                "error_type=%s",
                target_id,
                type(exc).__name__,
            )
            raise RuntimeUnavailableError(
                "The Runtime Target did not report its kernels."
            ) from exc
