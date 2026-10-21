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
    MAINTENANCE_RUN_NOT_FOUND = "MAINTENANCE_RUN_NOT_FOUND"
    MAINTENANCE_RUN_CONFLICT = "MAINTENANCE_RUN_CONFLICT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class DomainError(Exception):
    """Base class for expected business errors."""

    code = ErrorCode.INTERNAL_ERROR
    # The REST status for this error; transports without HTTP ignore it.
    http_status = 400


class ExecutionNotFoundError(DomainError):
    code = ErrorCode.EXECUTION_NOT_FOUND
    http_status = 404


class ExecutionAttemptNotFoundError(DomainError):
    code = ErrorCode.EXECUTION_ATTEMPT_NOT_FOUND
    http_status = 404


class ExecutionOperationNotFoundError(DomainError):
    code = ErrorCode.EXECUTION_OPERATION_NOT_FOUND
    http_status = 404


class ExecutionNotebookNotAvailableError(DomainError):
    code = ErrorCode.EXECUTION_NOTEBOOK_NOT_AVAILABLE
    http_status = 409


class NotebookCellNotFoundError(DomainError):
    code = ErrorCode.NOTEBOOK_CELL_NOT_FOUND
    http_status = 404


class NotebookReadError(DomainError):
    code = ErrorCode.NOTEBOOK_READ_ERROR
    http_status = 422


class InvalidStateTransitionError(DomainError):
    code = ErrorCode.INVALID_STATE_TRANSITION
    http_status = 409


class ExecutionVersionConflictError(DomainError):
    code = ErrorCode.EXECUTION_VERSION_CONFLICT
    http_status = 409


class IdempotencyConflictError(DomainError):
    code = ErrorCode.IDEMPOTENCY_CONFLICT
    http_status = 409


class InvalidExecutionSpecError(DomainError):
    code = ErrorCode.INVALID_EXECUTION_SPEC
    http_status = 422


class UnsupportedRuntimeProfileError(DomainError):
    code = ErrorCode.UNSUPPORTED_RUNTIME_PROFILE
    http_status = 422


class InvalidCursorError(DomainError):
    code = ErrorCode.INVALID_CURSOR
    http_status = 422


class PersistenceConflictError(DomainError):
    """A uniqueness or optimistic concurrency constraint was violated."""

    code = ErrorCode.PERSISTENCE_CONFLICT
    http_status = 409


class RuntimeTargetNotFoundError(DomainError):
    code = ErrorCode.RUNTIME_TARGET_NOT_FOUND
    http_status = 404


class RuntimeTargetConfigurationError(DomainError):
    code = ErrorCode.RUNTIME_TARGET_CONFIGURATION_ERROR


class RuntimeTargetPurgeConflictError(DomainError):
    code = ErrorCode.RUNTIME_TARGET_PURGE_CONFLICT
    http_status = 409


class MaintenanceRunNotFoundError(DomainError):
    code = ErrorCode.MAINTENANCE_RUN_NOT_FOUND
    http_status = 404


class MaintenanceRunConflictError(DomainError):
    code = ErrorCode.MAINTENANCE_RUN_CONFLICT
    http_status = 409


class ExecutionArtifactNotFoundError(DomainError):
    code = ErrorCode.ARTIFACT_NOT_FOUND
    http_status = 404


class ArtifactRegistrationError(DomainError):
    code = ErrorCode.ARTIFACT_REGISTRATION_ERROR


class ArtifactContentUnavailableError(DomainError):
    code = ErrorCode.ARTIFACT_CONTENT_UNAVAILABLE
    http_status = 409


class ArtifactRangeNotSatisfiableError(DomainError):
    code = ErrorCode.ARTIFACT_RANGE_NOT_SATISFIABLE
    http_status = 416

    def __init__(self, message: str, size_bytes: int) -> None:
        super().__init__(message)
        self.size_bytes = size_bytes
