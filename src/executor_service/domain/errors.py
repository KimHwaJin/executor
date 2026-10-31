"""Errors understood by the application and interface layers."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Self


class ErrorCode(StrEnum):
    REQUEST_VALIDATION_ERROR = "REQUEST_VALIDATION_ERROR"
    REQUEST_BODY_TOO_LARGE = "REQUEST_BODY_TOO_LARGE"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    INVALID_IDENTITY = "INVALID_IDENTITY"
    FORBIDDEN = "FORBIDDEN"
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
    UNSUPPORTED_RUNTIME_CAPABILITY = "UNSUPPORTED_RUNTIME_CAPABILITY"
    RUNTIME_TARGET_CONFIGURATION_ERROR = "RUNTIME_TARGET_CONFIGURATION_ERROR"
    ARTIFACT_REGISTRATION_ERROR = "ARTIFACT_REGISTRATION_ERROR"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    EXECUTION_VERSION_CONFLICT = "EXECUTION_VERSION_CONFLICT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    PERSISTENCE_CONFLICT = "PERSISTENCE_CONFLICT"
    RUNTIME_TARGET_PURGE_CONFLICT = "RUNTIME_TARGET_PURGE_CONFLICT"
    RUNTIME_TARGET_ENDPOINT_CONFLICT = "RUNTIME_TARGET_ENDPOINT_CONFLICT"
    MAINTENANCE_RUN_NOT_FOUND = "MAINTENANCE_RUN_NOT_FOUND"
    MAINTENANCE_RUN_CONFLICT = "MAINTENANCE_RUN_CONFLICT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


ContextValue = str | int | float | bool | None


@dataclass(frozen=True, slots=True)
class ErrorIssue:
    """One field-level problem, in the shape request validation also uses.

    ``location`` starts with where the value came from (``body`` or
    ``header``) and follows the request's own field names, so a caller can
    point at the exact value. ``type`` is a stable snake_case reason and
    ``context`` carries the numbers behind it, such as a limit and the
    value that broke it.
    """

    location: tuple[str | int, ...]
    message: str
    type: str
    context: Mapping[str, ContextValue] | None = field(default=None)


class DomainError(Exception):
    """Base class for expected business errors."""

    code = ErrorCode.INTERNAL_ERROR
    # The REST status for this error; transports without HTTP ignore it.
    http_status = 400

    def __init__(
        self, message: str = "", *, details: Sequence[ErrorIssue] = ()
    ) -> None:
        super().__init__(message)
        self.details: tuple[ErrorIssue, ...] = tuple(details)

    @classmethod
    def at(
        cls,
        location: Sequence[str | int],
        message: str,
        type: str,
        **context: ContextValue,
    ) -> Self:
        """This error with one ErrorIssue whose message is also the error's."""
        return cls(
            message,
            details=(
                ErrorIssue(tuple(location), message, type, context or None),
            ),
        )


class UnauthenticatedError(DomainError):
    code = ErrorCode.UNAUTHENTICATED
    http_status = 401


class InvalidIdentityError(DomainError):
    code = ErrorCode.INVALID_IDENTITY
    http_status = 400


class ForbiddenError(DomainError):
    code = ErrorCode.FORBIDDEN
    http_status = 403


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


class UnsupportedRuntimeCapabilityError(DomainError):
    code = ErrorCode.UNSUPPORTED_RUNTIME_CAPABILITY
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


class RuntimeTargetEndpointConflictError(DomainError):
    code = ErrorCode.RUNTIME_TARGET_ENDPOINT_CONFLICT
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
