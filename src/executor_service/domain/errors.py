"""Errors understood by the application and interface layers."""

from enum import StrEnum


class ErrorCode(StrEnum):
    REQUEST_VALIDATION_ERROR = "REQUEST_VALIDATION_ERROR"
    EXECUTION_NOT_FOUND = "EXECUTION_NOT_FOUND"
    EXECUTION_ATTEMPT_NOT_FOUND = "EXECUTION_ATTEMPT_NOT_FOUND"
    EXECUTION_OPERATION_NOT_FOUND = "EXECUTION_OPERATION_NOT_FOUND"
    EXECUTION_NOTEBOOK_NOT_AVAILABLE = "EXECUTION_NOTEBOOK_NOT_AVAILABLE"
    NOTEBOOK_CELL_NOT_FOUND = "NOTEBOOK_CELL_NOT_FOUND"
    NOTEBOOK_READ_ERROR = "NOTEBOOK_READ_ERROR"
    ARTIFACT_NOT_FOUND = "ARTIFACT_NOT_FOUND"
    ARTIFACT_CONTENT_UNAVAILABLE = "ARTIFACT_CONTENT_UNAVAILABLE"
    ARTIFACT_RANGE_NOT_SATISFIABLE = "ARTIFACT_RANGE_NOT_SATISFIABLE"
    RUNTIME_TARGET_NOT_FOUND = "RUNTIME_TARGET_NOT_FOUND"
    INVALID_EXECUTION_SPEC = "INVALID_EXECUTION_SPEC"
    INVALID_CURSOR = "INVALID_CURSOR"
    UNSUPPORTED_RUNTIME_PROFILE = "UNSUPPORTED_RUNTIME_PROFILE"
    RUNTIME_TARGET_CONFIGURATION_ERROR = "RUNTIME_TARGET_CONFIGURATION_ERROR"
    ARTIFACT_REGISTRATION_ERROR = "ARTIFACT_REGISTRATION_ERROR"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    EXECUTION_VERSION_CONFLICT = "EXECUTION_VERSION_CONFLICT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    PERSISTENCE_CONFLICT = "PERSISTENCE_CONFLICT"
    RUNTIME_TARGET_PURGE_CONFLICT = "RUNTIME_TARGET_PURGE_CONFLICT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class DomainError(Exception):
    """Base class for expected business errors."""

    code = ErrorCode.INTERNAL_ERROR


class ExecutionNotFoundError(DomainError):
    code = ErrorCode.EXECUTION_NOT_FOUND


class ExecutionAttemptNotFoundError(DomainError):
    code = ErrorCode.EXECUTION_ATTEMPT_NOT_FOUND


class ExecutionOperationNotFoundError(DomainError):
    code = ErrorCode.EXECUTION_OPERATION_NOT_FOUND


class ExecutionNotebookNotAvailableError(DomainError):
    code = ErrorCode.EXECUTION_NOTEBOOK_NOT_AVAILABLE


class NotebookCellNotFoundError(DomainError):
    code = ErrorCode.NOTEBOOK_CELL_NOT_FOUND


class NotebookReadError(DomainError):
    code = ErrorCode.NOTEBOOK_READ_ERROR


class InvalidStateTransitionError(DomainError):
    code = ErrorCode.INVALID_STATE_TRANSITION


class ExecutionVersionConflictError(DomainError):
    code = ErrorCode.EXECUTION_VERSION_CONFLICT


class IdempotencyConflictError(DomainError):
    code = ErrorCode.IDEMPOTENCY_CONFLICT


class InvalidExecutionSpecError(DomainError):
    code = ErrorCode.INVALID_EXECUTION_SPEC


class UnsupportedRuntimeProfileError(DomainError):
    code = ErrorCode.UNSUPPORTED_RUNTIME_PROFILE


class InvalidCursorError(DomainError):
    code = ErrorCode.INVALID_CURSOR


class PersistenceConflictError(DomainError):
    """A uniqueness or optimistic concurrency constraint was violated."""

    code = ErrorCode.PERSISTENCE_CONFLICT


class RuntimeTargetNotFoundError(DomainError):
    code = ErrorCode.RUNTIME_TARGET_NOT_FOUND


class RuntimeTargetConfigurationError(DomainError):
    code = ErrorCode.RUNTIME_TARGET_CONFIGURATION_ERROR


class RuntimeTargetPurgeConflictError(DomainError):
    code = ErrorCode.RUNTIME_TARGET_PURGE_CONFLICT


class ExecutionArtifactNotFoundError(DomainError):
    code = ErrorCode.ARTIFACT_NOT_FOUND


class ArtifactRegistrationError(DomainError):
    code = ErrorCode.ARTIFACT_REGISTRATION_ERROR


class ArtifactContentUnavailableError(DomainError):
    code = ErrorCode.ARTIFACT_CONTENT_UNAVAILABLE


class ArtifactRangeNotSatisfiableError(DomainError):
    code = ErrorCode.ARTIFACT_RANGE_NOT_SATISFIABLE

    def __init__(self, message: str, size_bytes: int) -> None:
        super().__init__(message)
        self.size_bytes = size_bytes
