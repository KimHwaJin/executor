"""Pure mapping from execution failures to durable failure policy."""

from executor_service.domain.enums import FailureType, RetryStrategy
from executor_service.domain.runtime import (
    ExecutionCompletionError,
    RuntimeDriverError,
    RuntimeExecutionError,
    RuntimeExecutionTimeoutError,
    RuntimeOutputLimitExceededError,
)
from executor_service.infrastructure.execution_worker.types import (
    RetainedRuntimeSessionLostError,
    StoredStepFailure,
)
from executor_service.infrastructure.runtime_diagnostics import failure_message


def failure_policy(
    exc: Exception, retain_session: bool
) -> tuple[FailureType, RetryStrategy]:
    if isinstance(exc, StoredStepFailure):
        return failure_policy(exc.original, retain_session)
    if isinstance(exc, ExecutionCompletionError):
        return FailureType.COMPLETION_FAILED, RetryStrategy.NOT_RETRYABLE
    if isinstance(exc, RetainedRuntimeSessionLostError):
        return FailureType.RUNTIME_SESSION_LOST, RetryStrategy.FROM_START
    if isinstance(exc, RuntimeExecutionTimeoutError):
        failure_type = (
            FailureType.STEP_TIMEOUT
            if exc.scope == "Step"
            else FailureType.OPERATION_TIMEOUT
        )
        retry_strategy = (
            RetryStrategy.FROM_FAILED_STEP
            if retain_session
            else RetryStrategy.FROM_START
        )
        return failure_type, retry_strategy
    if isinstance(exc, RuntimeOutputLimitExceededError):
        retry_strategy = (
            RetryStrategy.FROM_FAILED_STEP
            if retain_session
            else RetryStrategy.FROM_START
        )
        return FailureType.OUTPUT_LIMIT_EXCEEDED, retry_strategy
    if isinstance(exc, RuntimeExecutionError):
        return FailureType.TOOL_ERROR, (
            RetryStrategy.FROM_FAILED_STEP
            if retain_session
            else RetryStrategy.FROM_START
        )
    if isinstance(exc, RuntimeDriverError):
        return FailureType.RUNTIME_UNAVAILABLE, RetryStrategy.FROM_START
    return FailureType.INTERNAL_ERROR, RetryStrategy.NOT_RETRYABLE


def safe_error(exc: Exception) -> str:
    if isinstance(exc, StoredStepFailure):
        return safe_error(exc.original)
    return failure_message(exc)
