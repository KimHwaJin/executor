"""Executor domain enumerations."""

from enum import StrEnum


class ExecutionStatus(StrEnum):
    QUEUED = "QUEUED"
    DISPATCHED = "DISPATCHED"
    RUNNING = "RUNNING"
    WAITING_FOR_CONTINUE = "WAITING_FOR_CONTINUE"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        return self in {self.CANCELLED, self.SUCCEEDED, self.FAILED}


class ExecutionMode(StrEnum):
    STATIC = "STATIC"
    DYNAMIC = "DYNAMIC"


class TriggerType(StrEnum):
    INTERACTIVE = "INTERACTIVE"
    BATCH = "BATCH"


class ActorType(StrEnum):
    USER = "USER"
    BATCH = "BATCH"


class RuntimePool(StrEnum):
    INTERACTIVE = "INTERACTIVE"
    BATCH = "BATCH"


class RuntimeType(StrEnum):
    JUPYTER = "JUPYTER"


class CodeSourceType(StrEnum):
    INLINE = "INLINE"
    PATH = "PATH"


class StepStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


class OutboxStatus(StrEnum):
    PENDING = "PENDING"
    PUBLISHED = "PUBLISHED"


class RuntimeTargetStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DRAINING = "DRAINING"
    OFFLINE = "OFFLINE"


class AttemptStatus(StrEnum):
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class OperationStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in {self.SUCCEEDED, self.FAILED, self.CANCELLED}


class FailureType(StrEnum):
    TOOL_ERROR = "TOOL_ERROR"
    INFRASTRUCTURE_ERROR = "INFRASTRUCTURE_ERROR"
    WORKER_SHUTDOWN = "WORKER_SHUTDOWN"
    RUNTIME_UNAVAILABLE = "RUNTIME_UNAVAILABLE"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    DYNAMIC_WAIT_TIMEOUT = "DYNAMIC_WAIT_TIMEOUT"
    EXECUTION_TIMEOUT = "EXECUTION_TIMEOUT"
    RUNTIME_SESSION_LOST = "RUNTIME_SESSION_LOST"


class RetryStrategy(StrEnum):
    NOT_RETRYABLE = "NOT_RETRYABLE"
    FROM_FAILED_STEP = "FROM_FAILED_STEP"
    FROM_START = "FROM_START"


class RuntimeSessionCleanupStatus(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ArtifactType(StrEnum):
    DATASET = "DATASET"
    NOTEBOOK = "NOTEBOOK"
    REPORT = "REPORT"
    PLOT = "PLOT"
    MODEL = "MODEL"
    METRIC = "METRIC"
    LOG = "LOG"
    OTHER = "OTHER"


class ArtifactStorageType(StrEnum):
    PV = "PV"
    S3 = "S3"


class ArtifactStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    INCOMPLETE = "INCOMPLETE"
    DELETED = "DELETED"
