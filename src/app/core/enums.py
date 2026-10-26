from enum import StrEnum


class DeleteYN(StrEnum):
    N = "N"
    Y = "Y"


class ProjectMemberRole(StrEnum):
    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"


class MessageType(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    # Agent의 중간 판단/작업 결과. 최종 사용자 답변인 assistant와 구분한다.
    AGENT = "agent"
    TOOL = "tool"


class MessageStatus(StrEnum):
    PENDING = "pending"
    STREAMING = "streaming"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class LLMRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"
    CANCELED = "canceled"


class TaskStatus(StrEnum):
    """최초 분석 요청부터 최종 종료까지 유지되는 논리적 Job 상태입니다."""

    PENDING = "pending"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"
    CANCELED = "canceled"


def enum_values(enum_cls):
    """SQLAlchemy Enum이 member name이 아니라 실제 value를 저장하게 합니다."""
    return [member.value for member in enum_cls]

