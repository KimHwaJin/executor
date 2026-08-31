"""Safe classification for persisted diagnostics; never copy raw payloads."""

from executor_service.domain.diagnostics import (
    DiagnosticCategory,
    DiagnosticCause,
    DiagnosticOrigin,
    RuntimeDiagnostic,
)
from executor_service.domain.runtime import (
    ExecutionCompletionError,
    RuntimeDriverError,
    RuntimeExecutionError,
    RuntimeExecutionTimeoutError,
    RuntimeOutputLimitExceededError,
)
from executor_service.infrastructure.runtime_diagnostics import failure_message


def diagnostic_for(
    error: BaseException, *, phase: str, category: DiagnosticCategory
) -> RuntimeDiagnostic:
    causes: list[DiagnosticCause] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen and len(causes) < 8:
        seen.add(id(current))
        causes.append(
            DiagnosticCause(
                exception_type=type(current).__name__[:128],
                message=_message(current),
                errno=current.errno if isinstance(current, OSError) else None,
            )
        )
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    code = "INTERNAL_ERROR"
    origin = DiagnosticOrigin.UNKNOWN
    if phase.startswith("RESULT_"):
        origin = DiagnosticOrigin.RESULT_STORAGE
    elif isinstance(error, RuntimeDriverError):
        origin = DiagnosticOrigin.RUNTIME
    elif phase in {"NOTEBOOK_BUILD", "ARTIFACT_REGISTER"}:
        origin = DiagnosticOrigin.EXECUTOR
    if isinstance(error, ExecutionCompletionError):
        code = "COMPLETION_FAILED"
        origin = DiagnosticOrigin.EXECUTOR
        phase = error.phase
    elif isinstance(error, RuntimeOutputLimitExceededError):
        code = f"OUTPUT_{error.kind}_LIMIT_EXCEEDED"
    elif isinstance(error, RuntimeExecutionTimeoutError):
        code = f"{error.scope.upper()}_TIMEOUT"
    elif isinstance(error, RuntimeExecutionError):
        code = "CODE_EXECUTION_FAILED"
    elif isinstance(error, PermissionError):
        code = "PERMISSION_DENIED"
    elif isinstance(error, FileNotFoundError):
        code = "FILE_NOT_FOUND"
    elif isinstance(error, TimeoutError):
        code = "TIMEOUT"
    elif isinstance(error, OSError):
        code = "OS_ERROR"
    elif isinstance(error, RuntimeDriverError):
        code = "RUNTIME_UNAVAILABLE"
    return RuntimeDiagnostic(
        code=code,
        phase=phase[:64],
        category=category,
        origin=origin,
        message=_message(error),
        causes=tuple(causes),
        causes_truncated=current is not None,
    )


def _message(error: BaseException) -> str:
    if isinstance(error, RuntimeExecutionError) and not isinstance(
        error, (RuntimeExecutionTimeoutError, RuntimeOutputLimitExceededError)
    ):
        return "Code execution failed; inspect Step result files."
    return failure_message(error)
