"""State contract shared by the test Agent graph nodes."""

from typing import Annotated, Any, Literal

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field


class AgentState(BaseModel):
    messages: Annotated[list[AnyMessage], add_messages] = Field(default_factory=list)
    phase: Literal[
        "BOOTSTRAP",
        "READY",
        "SUBMITTING",
        "WAITING_FOR_EVENT",
        "VERIFYING",
        "ADVANCING",
        "SUCCEEDED",
        "FAILED",
    ] = "BOOTSTRAP"
    execution_id: str | None = None
    execution_request: dict[str, Any] | None = None
    event_batch: dict[str, Any] | None = None
    event_history: list[dict[str, Any]] = Field(default_factory=list)
    command_receipts: list[dict[str, Any]] = Field(default_factory=list)
    next_operation_index: int = Field(default=0, ge=0)
    execution_result: dict[str, Any] | None = None
    wait_strategy: Literal["INTERRUPT", "STREAM"] = "INTERRUPT"
    awaited_event_types: list[str] = Field(default_factory=list)
    awaited_operation_id: str | None = None
    event_stream_start_id: str | None = None
