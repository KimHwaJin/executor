"""LLM planning boundary for the natural-language integration scenario."""

import ast
import json
from typing import Any, Literal, Protocol

from langchain_core.messages import AnyMessage, BaseMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field, model_validator

PLANNER_SYSTEM_PROMPT = """
You are the planner for a local Agent -> Executor -> Jupyter integration test.
Return exactly one JSON object and no markdown fences.

Schema:
{
  "intent": "CHAT" | "EXECUTE",
  "response": "answer for CHAT, otherwise a short execution summary",
  "runtime_profile": "basic" | "ml",
  "steps": [
    {
      "skill_name": "data_io|data_load|data_preprocess|eda|modeling|evaluation|report",
      "tool_name": "short_snake_case_name",
      "code": "complete Python code for one Jupyter cell"
    }
  ]
}

Use CHAT for explanations, greetings, and questions that do not need Python execution. Answer the
user directly in response and return an empty steps array.

Use EXECUTE when the user asks to calculate, analyze, plot, train, evaluate, generate a file, or
actually run Python. Create one to five ordered cells. Cells share one Jupyter kernel, so later
cells may use variables from earlier cells. Print the final human-readable result. If a requested
file is produced, write it below a relative artifacts/<type>/ directory. Use runtime_profile ml
only when the request needs ML-specific packages; otherwise use basic.

This is a constrained local test. Generated code must not access environment variables, secrets,
network services, subprocesses, shell commands, or absolute filesystem paths.
""".strip()

BLOCKED_IMPORT_ROOTS = {
    "asyncio",
    "http",
    "httpx",
    "multiprocessing",
    "os",
    "requests",
    "shutil",
    "socket",
    "subprocess",
    "sys",
    "urllib",
}
BLOCKED_CALL_NAMES = {"__import__", "breakpoint", "compile", "eval", "exec"}


class PlannedStep(BaseModel):
    """One LLM-planned Jupyter cell."""

    model_config = ConfigDict(extra="forbid")

    skill_name: Literal[
        "data_io",
        "data_load",
        "data_preprocess",
        "eda",
        "modeling",
        "evaluation",
        "report",
    ]
    tool_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    code: str = Field(min_length=1, max_length=50_000)

    @model_validator(mode="after")
    def reject_unsafe_test_code(self) -> "PlannedStep":
        """Reject obvious host, network, process, and dynamic-code access in this test harness."""
        try:
            tree = ast.parse(self.code)
        except SyntaxError as exc:
            raise ValueError("Planned Step code must be valid Python.") from exc
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.partition(".")[0] for alias in node.names}
                if roots & BLOCKED_IMPORT_ROOTS:
                    raise ValueError("Planned Step imports a blocked module.")
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").partition(".")[0]
                if root in BLOCKED_IMPORT_ROOTS:
                    raise ValueError("Planned Step imports a blocked module.")
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in BLOCKED_CALL_NAMES
            ):
                raise ValueError("Planned Step calls a blocked dynamic-code function.")
        return self


class PlanningDecision(BaseModel):
    """Strict transport-independent result returned by the test planner."""

    model_config = ConfigDict(extra="forbid")

    intent: Literal["CHAT", "EXECUTE"]
    response: str = ""
    runtime_profile: Literal["basic", "ml"] = "basic"
    steps: list[PlannedStep] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def validate_intent_payload(self) -> "PlanningDecision":
        if self.intent == "EXECUTE" and not self.steps:
            raise ValueError("EXECUTE decisions require at least one Step.")
        if self.intent == "CHAT" and self.steps:
            raise ValueError("CHAT decisions cannot contain Steps.")
        return self


class PlannerModel(Protocol):
    """Small model boundary that remains fakeable without depending on ChatOpenAI internals."""

    async def ainvoke(self, input: Any) -> BaseMessage: ...


async def plan_message(model: PlannerModel, messages: list[AnyMessage]) -> PlanningDecision:
    """Ask an OpenAI-compatible model for JSON and validate the complete response."""
    raw = await model.ainvoke([SystemMessage(content=PLANNER_SYSTEM_PROMPT), *messages])
    content = _message_text(raw)
    try:
        payload = json.loads(_strip_markdown_fence(content))
    except json.JSONDecodeError as exc:
        raise ValueError("The LLM planner did not return valid JSON.") from exc
    return PlanningDecision.model_validate(payload)


def _message_text(message: BaseMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    text_parts: list[str] = []
    for part in message.content:
        if isinstance(part, str):
            text_parts.append(part)
        elif isinstance(part, dict) and isinstance(part.get("text"), str):
            text_parts.append(part["text"])
    if not text_parts:
        raise ValueError("The LLM planner response did not contain text.")
    return "".join(text_parts)


def _strip_markdown_fence(content: str) -> str:
    stripped = content.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) >= 3 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return stripped


def decision_steps(decision: PlanningDecision) -> list[dict[str, Any]]:
    """Convert validated planner Steps into the Agent execution boundary."""
    return [step.model_dump(mode="json") for step in decision.steps]
