"""Executor domain enumerations."""

from enum import StrEnum


class ExecutionStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_FOR_OPERATION = "WAITING_FOR_OPERATION"
    FINALIZING = "FINALIZING"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        return self in {self.CANCELLED, self.SUCCEEDED, self.FAILED}


class OperationMode(StrEnum):
    SINGLE = "SINGLE"
    MULTI = "MULTI"


class TriggerType(StrEnum):
    INTERACTIVE = "INTERACTIVE"
    BATCH = "BATCH"


class ExecutorAdmissionState(StrEnum):
    ACTIVE = "ACTIVE"
    DRAINING = "DRAINING"


class MaintenanceRunAction(StrEnum):
    STOP_ACTIVE_EXECUTIONS = "STOP_ACTIVE_EXECUTIONS"


class MaintenanceRunStatus(StrEnum):
    REQUESTED = "REQUESTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        return self in {self.SUCCEEDED, self.FAILED}


class MaintenanceRunTargetStatus(StrEnum):
    PENDING = "PENDING"
    STOP_REQUESTED = "STOP_REQUESTED"
    STOPPED = "STOPPED"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        return self in {self.STOPPED, self.FAILED}


class RuntimePool(StrEnum):
    INTERACTIVE = "INTERACTIVE"
    BATCH = "BATCH"


class RuntimeType(StrEnum):
    JUPYTER = "JUPYTER"


class CodeSourceType(StrEnum):
    INLINE = "INLINE"
    PATH = "PATH"


class StepPayloadType(StrEnum):
    PYTHON_EXECUTE = "PYTHON_EXECUTE"


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
    OPERATION_WAIT_TIMEOUT = "OPERATION_WAIT_TIMEOUT"
    OPERATION_TIMEOUT = "OPERATION_TIMEOUT"
    STEP_TIMEOUT = "STEP_TIMEOUT"
    EXECUTION_TIMEOUT = "EXECUTION_TIMEOUT"
    QUEUE_TIMEOUT = "QUEUE_TIMEOUT"
    OUTPUT_LIMIT_EXCEEDED = "OUTPUT_LIMIT_EXCEEDED"
    RUNTIME_SESSION_LOST = "RUNTIME_SESSION_LOST"
    COMPLETION_FAILED = "COMPLETION_FAILED"


class FinalizedBy(StrEnum):
    """What ended a MULTI Execution, or any Execution past its lifetime."""

    AGENT = "AGENT"
    WAIT_TIMEOUT = "WAIT_TIMEOUT"
    MAX_RUNTIME = "MAX_RUNTIME"


class StatusReasonCode(StrEnum):
    """Why an Execution is in its status, where no other enum says so.

    A FAILED Execution's reason is its FailureType, and one ended by
    finalization or the maximum runtime carries its FinalizedBy.
    """

    # QUEUED: what keeps a Worker from starting it.
    ADMISSION_DRAINING = "ADMISSION_DRAINING"
    NO_ACTIVE_TARGET = "NO_ACTIVE_TARGET"
    PROFILE_UNSUPPORTED = "PROFILE_UNSUPPORTED"
    CAPACITY_FULL = "CAPACITY_FULL"
    MEMORY_LIMIT = "MEMORY_LIMIT"
    TARGET_UNAVAILABLE = "TARGET_UNAVAILABLE"
    WAITING_FOR_WORKER = "WAITING_FOR_WORKER"
    # RUNNING, while a released Step waits for another Worker.
    WORKER_HANDOVER = "WORKER_HANDOVER"
    AWAITING_NEXT_OPERATION = "AWAITING_NEXT_OPERATION"
    CANCELLED_BY_REQUEST = "CANCELLED_BY_REQUEST"
    COMPLETED = "COMPLETED"


class HandoverReason(StrEnum):
    EXECUTOR_SHUTDOWN = "EXECUTOR_SHUTDOWN"
    EXECUTOR_UNRESPONSIVE = "EXECUTOR_UNRESPONSIVE"


class RetryStrategy(StrEnum):
    NOT_RETRYABLE = "NOT_RETRYABLE"
    FROM_FAILED_STEP = "FROM_FAILED_STEP"
    FROM_START = "FROM_START"


class RuntimeSessionCleanupStatus(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class RuntimeAbortStatus(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    IDLE_CONFIRMED = "IDLE_CONFIRMED"
    SESSION_DELETED = "SESSION_DELETED"
    SESSION_MISSING = "SESSION_MISSING"
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
