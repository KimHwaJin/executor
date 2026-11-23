"""Which kernel an Execution runs on, and what that kernel uses.

Live values are read from the Runtime on request, never stored or polled:
any Executor replica answers the same. Recorded values are each Step
attempt's peak memory and CPU time, saved when the Step finished.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from executor_service.application.execution_queries import (
    ExecutionDetailView,
)
from executor_service.domain.enums import ExecutionStatus
from executor_service.domain.runtime import RuntimeKernelUsage

# NO_KERNEL: the Execution holds no kernel now (not started, or released).
# NOT_RUNNING: its Runtime Target no longer runs that kernel.
# RUNTIME_UNAVAILABLE: the Runtime Target could not be asked.
KernelState = Literal[
    "RUNNING", "NO_KERNEL", "NOT_RUNNING", "RUNTIME_UNAVAILABLE"
]


@dataclass(frozen=True, slots=True)
class RecordedStepUsageView:
    peak_memory_bytes: int | None
    cpu_seconds: float | None
    measured_step_attempt_count: int


@dataclass(frozen=True, slots=True)
class ExecutionKernelUsageView:
    execution_id: UUID
    target_id: UUID | None
    kernel_id: str | None
    kernel_state: KernelState
    kernel: RuntimeKernelUsage | None
    observed_at: datetime | None
    steps: RecordedStepUsageView


@dataclass(frozen=True, slots=True)
class TargetKernelView:
    usage: RuntimeKernelUsage
    # Null for a kernel no Execution holds, such as one left behind.
    execution_id: UUID | None
    tenant_id: str | None
    execution_status: ExecutionStatus | None


@dataclass(frozen=True, slots=True)
class RuntimeTargetKernelsView:
    target_id: UUID
    observed_at: datetime
    kernels: tuple[TargetKernelView, ...]


class KernelUsageQueries(Protocol):
    async def execution_usage(
        self, execution: ExecutionDetailView
    ) -> ExecutionKernelUsageView: ...

    async def target_kernels(
        self, target_id: UUID
    ) -> RuntimeTargetKernelsView: ...
