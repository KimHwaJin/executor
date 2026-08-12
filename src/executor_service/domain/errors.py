"""Errors understood by the application and interface layers."""


class DomainError(Exception):
    """Base class for expected business errors."""


class ExecutionNotFoundError(DomainError):
    pass


class InvalidStateTransitionError(DomainError):
    pass


class ExecutionVersionConflictError(DomainError):
    pass


class IdempotencyConflictError(DomainError):
    pass


class InvalidExecutionSpecError(DomainError):
    pass


class UnsupportedRuntimeProfileError(DomainError):
    pass


class InvalidCursorError(DomainError):
    pass


class PersistenceConflictError(DomainError):
    """A uniqueness or optimistic concurrency constraint was violated."""


class RuntimeTargetNotFoundError(DomainError):
    pass


class RuntimeTargetConfigurationError(DomainError):
    pass


class RuntimeTargetPurgeConflictError(DomainError):
    pass


class ExecutionArtifactNotFoundError(DomainError):
    pass


class ArtifactRegistrationError(DomainError):
    pass
