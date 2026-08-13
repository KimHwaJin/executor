"""State contract shared by the test Agent graph nodes."""

from typing import Annotated, Literal

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel


class AgentState(BaseModel):
    messages: Annotated[list[AnyMessage], add_messages]
    phase: Literal["BOOTSTRAP", "READY"]
    execution_id: str | None
