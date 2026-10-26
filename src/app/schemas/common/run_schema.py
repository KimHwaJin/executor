from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.core.enums import AgentRunStatus
from app.schemas.schema_base import ORMModel


class RunInput(BaseModel):
    messages: list[dict[str, Any]] = Field(min_length=1)


class RunCreate(BaseModel):
    input: RunInput | None = None
    # 대부분의 HITL은 JSON 객체지만 로컬 mock 데이터 선택은 "mock" 문자열을 사용합니다.
    command: dict[str, Any] | str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    multitask_strategy: Literal["reject"] = "reject"
    stream_mode: list[Literal["messages", "updates", "values", "custom"]] = Field(default_factory=list)
    stream_resumable: bool = True
    on_disconnect: Literal["continue"] = "continue"

    @model_validator(mode="after")
    def input_xor_command(self):
        if (self.input is None) == (self.command is None):
            raise ValueError("Exactly one of input or command is required.")
        return self


class RunResource(ORMModel):
    id: UUID = Field(validation_alias="run_id")
    # resume 요청으로 새 AgentRun row가 생겨도 실제 LangGraph thread는 최초 Run을 사용합니다.
    checkpoint_run_id: UUID | None = None
    task_id: UUID | None = None
    session_id: UUID
    status: AgentRunStatus
    # DB/RunService의 interrupt_payload()와 동일하게 복수 interrupt 배열을 노출합니다.
    interrupt: list[dict[str, Any]] | None
    failure: dict[str, Any] | None
    attempt_count: int
    next_attempt_at: datetime | None
    cancel_reason: str | None
    cancel_requested_at: datetime | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    mock_llm_sleep_seconds: float | None = None
    node_timings: dict[str, Any] | None = None
    queue_wait_seconds: float | None = None

    @model_validator(mode="before")
    @classmethod
    def read_load_metrics(cls, value):
        metadata = getattr(value, "metadata_json", None)
        try:
            if isinstance(metadata, dict):
                if "mock_llm_sleep_seconds" in metadata:
                    setattr(value, "mock_llm_sleep_seconds", metadata["mock_llm_sleep_seconds"])
                if isinstance(metadata.get("node_timings"), dict):
                    setattr(value, "node_timings", metadata["node_timings"])
            created_at = getattr(value, "created_at", None)
            started_at = getattr(value, "started_at", None)
            if created_at is not None and started_at is not None:
                setattr(
                    value,
                    "queue_wait_seconds",
                    max(0.0, (started_at - created_at).total_seconds()),
                )
        except Exception:
            return value
        return value


class RunCancel(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


class AgentRunLogResource(ORMModel):
    """프런트 진행 화면에서 표시할 Agent 실행 로그입니다."""

    log_id: UUID
    run_id: UUID
    event_key: str
    agent_name: str | None
    node: str
    event: str
    kind: str
    payload: dict[str, Any]
    created_at: datetime

