"""State contract for the conversation, plan review, and execution planning Agent."""

from typing import Annotated, Any, Literal

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field


class PlanningAgentState(BaseModel):
    messages: Annotated[list[AnyMessage], add_messages] = Field(default_factory=list)
    phase: Literal[
        "ROUTING",
        "CHATTING",
        "PLANNING",
        "AWAITING_APPROVAL",
        "SUBMITTING",
        "WAITING_FOR_EVENT",
        "VERIFYING",
        "ADVANCING",
        "READY",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
    ] = "ROUTING"
    intent: Literal["CHAT", "EXECUTION"] | None = None
    request_scope_id: str | None = None
    plan: dict[str, Any] | None = None
    approved_plan: dict[str, Any] | None = None
    execution_request: dict[str, Any] | None = None
    execution_id: str | None = None
    event_batch: dict[str, Any] | None = None
    event_history: list[dict[str, Any]] = Field(default_factory=list)
    command_receipts: list[dict[str, Any]] = Field(default_factory=list)
    next_operation_index: int = Field(default=0, ge=0)
    execution_result: dict[str, Any] | None = None
    awaited_event_types: list[str] = Field(default_factory=list)
    awaited_operation_id: str | None = None
    event_stream_start_id: str | None = None
