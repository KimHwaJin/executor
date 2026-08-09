"""Executor domain enumerations."""

from enum import StrEnum


class ExecutionStatus(StrEnum):
    QUEUED = "QUEUED"
    DISPATCHED = "DISPATCHED"
    RUNNING = "RUNNING"
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


class JupyterPool(StrEnum):
    INTERACTIVE = "INTERACTIVE"
    BATCH = "BATCH"


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


class JupyterServerStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DRAINING = "DRAINING"
    OFFLINE = "OFFLINE"


class AttemptStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class FailureType(StrEnum):
    TOOL_ERROR = "TOOL_ERROR"
    INFRASTRUCTURE_ERROR = "INFRASTRUCTURE_ERROR"
    WORKER_SHUTDOWN = "WORKER_SHUTDOWN"
    JUPYTER_UNAVAILABLE = "JUPYTER_UNAVAILABLE"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class RetryStrategy(StrEnum):
    NOT_RETRYABLE = "NOT_RETRYABLE"
    FROM_FAILED_STEP = "FROM_FAILED_STEP"
    FROM_START = "FROM_START"


class KernelCleanupStatus(StrEnum):
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
