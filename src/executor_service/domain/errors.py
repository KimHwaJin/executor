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


class PersistenceConflictError(DomainError):
    """A uniqueness or optimistic concurrency constraint was violated."""


class JupyterServerNotFoundError(DomainError):
    pass


class JupyterServerConfigurationError(DomainError):
    pass


class ExecutionArtifactNotFoundError(DomainError):
    pass


class ArtifactRegistrationError(DomainError):
    pass
