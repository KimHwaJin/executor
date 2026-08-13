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
        "SUCCEEDED",
        "FAILED",
    ] = "BOOTSTRAP"
    execution_id: str | None = None
    execution_request: dict[str, Any] | None = None
    terminal_event: dict[str, Any] | None = None
    execution_result: dict[str, Any] | None = None
    wait_strategy: Literal["INTERRUPT", "STREAM"] = "INTERRUPT"
