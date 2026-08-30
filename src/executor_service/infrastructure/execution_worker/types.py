"""Private value types and stored-result errors used by execution workers."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from executor_service.domain.enums import (
    RetryStrategy,
    RuntimeAbortStatus,
    RuntimeSessionCleanupStatus,
)
from executor_service.domain.results import StepResultDescriptor
from executor_service.domain.runtime import (
    RuntimeDriverError,
    RuntimeExecutionError,
    RuntimeExecutionTimeoutError,
    RuntimeOutputLimitExceededError,
)
from executor_service.infrastructure.execution_leases import CancellationLease


class RetainedRuntimeSessionLostError(RuntimeDriverError):
    """Raised when a retained-session retry target no longer has its session."""


@dataclass(frozen=True, slots=True)
class RuntimeAbortResolution:
    abort_status: RuntimeAbortStatus
    cleanup_status: RuntimeSessionCleanupStatus
    retry_strategy: RetryStrategy
    retain_session: bool


@dataclass(frozen=True, slots=True)
class CancellationWork:
    lease: CancellationLease
    runtime_target_id: UUID | None
    runtime_session_id: str | None


@dataclass(frozen=True, slots=True)
class ExpiredLeaseRecovery:
    execution_count: int
    cleanup_targets: tuple[tuple[UUID, UUID | None, UUID, str], ...]


class StoredRuntimeExecutionError(RuntimeExecutionError):
    def __init__(
        self,
        message: str,
        outputs: list[dict[str, Any]],
        stored_result: StepResultDescriptor,
    ) -> None:
        super().__init__(message, outputs)
        self.stored_result = stored_result


class StoredStepFailure(Exception):
    """Transport/storage failure with any successfully sealed partial output.

    Not a RuntimeExecutionError: loss of a channel must not authorize reuse of a
    possibly still-busy kernel. The original exception determines failure policy.
    """

    def __init__(
        self,
        original: Exception,
        stored_result: StepResultDescriptor | None,
    ) -> None:
        super().__init__(type(original).__name__)
        self.original = original
        self.stored_result = stored_result


class StoredRuntimeExecutionTimeoutError(RuntimeExecutionTimeoutError):
    def __init__(
        self,
        scope: str,
        timeout_seconds: float,
        stored_result: StepResultDescriptor,
    ) -> None:
        super().__init__(scope, timeout_seconds)
        self.stored_result = stored_result


class StoredRuntimeOutputLimitExceededError(RuntimeOutputLimitExceededError):
    def __init__(
        self,
        max_message_bytes: int,
        stored_result: StepResultDescriptor,
    ) -> None:
        super().__init__(max_message_bytes)
        self.stored_result = stored_result
