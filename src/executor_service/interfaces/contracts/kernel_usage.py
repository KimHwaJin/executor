"""Kernel usage transport contracts."""

from datetime import datetime
from uuid import UUID

from executor_service.application.kernel_usage import (
    ExecutionKernelUsageView,
    KernelState,
    RuntimeTargetKernelsView,
)
from executor_service.domain.enums import ExecutionStatus
from executor_service.domain.runtime import RuntimeKernelUsage
from executor_service.interfaces.contracts.common import ContractModel


class KernelUsageResponse(ContractModel):
    """A running kernel now: its process and the processes it started.

    Memory is resident set size (RSS); pages shared with forked children
    count once per process. CPU seconds count from the kernel's start.
    """

    kernel_id: str
    execution_state: str | None
    process_count: int | None
    memory_bytes: int | None
    cpu_seconds: float | None

    @classmethod
    def from_usage(cls, usage: RuntimeKernelUsage) -> "KernelUsageResponse":
        return cls(
            kernel_id=usage.kernel_id,
            execution_state=usage.execution_state,
            process_count=usage.process_count,
            memory_bytes=usage.memory_bytes,
            cpu_seconds=usage.cpu_seconds,
        )


class RecordedStepUsageResponse(ContractModel):
    """Measured Step attempts of the Execution, over all its attempts."""

    peak_memory_bytes: int | None
    cpu_seconds: float | None
    measured_step_attempt_count: int


class ExecutionKernelUsageResponse(ContractModel):
    execution_id: UUID
    target_id: UUID | None
    kernel_id: str | None
    kernel_state: KernelState
    kernel: KernelUsageResponse | None
    observed_at: datetime | None
    steps: RecordedStepUsageResponse

    @classmethod
    def from_view(
        cls, view: ExecutionKernelUsageView
    ) -> "ExecutionKernelUsageResponse":
        return cls(
            execution_id=view.execution_id,
            target_id=view.target_id,
            kernel_id=view.kernel_id,
            kernel_state=view.kernel_state,
            kernel=(
                KernelUsageResponse.from_usage(view.kernel)
                if view.kernel is not None
                else None
            ),
            observed_at=view.observed_at,
            steps=RecordedStepUsageResponse(
                peak_memory_bytes=view.steps.peak_memory_bytes,
                cpu_seconds=view.steps.cpu_seconds,
                measured_step_attempt_count=(
                    view.steps.measured_step_attempt_count
                ),
            ),
        )


class TargetKernelResponse(KernelUsageResponse):
    # Null for a kernel no Execution holds, such as one left behind.
    execution_id: UUID | None
    tenant_id: str | None
    execution_status: ExecutionStatus | None


class RuntimeTargetKernelsResponse(ContractModel):
    target_id: UUID
    observed_at: datetime
    items: list[TargetKernelResponse]

    @classmethod
    def from_view(
        cls, view: RuntimeTargetKernelsView
    ) -> "RuntimeTargetKernelsResponse":
        return cls(
            target_id=view.target_id,
            observed_at=view.observed_at,
            items=[
                TargetKernelResponse(
                    **KernelUsageResponse.from_usage(
                        kernel.usage
                    ).model_dump(),
                    execution_id=kernel.execution_id,
                    tenant_id=kernel.tenant_id,
                    execution_status=kernel.execution_status,
                )
                for kernel in view.kernels
            ],
        )
