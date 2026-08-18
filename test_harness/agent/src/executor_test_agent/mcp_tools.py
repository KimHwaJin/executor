"""MCP 2.x to LangChain Tool bridge with an explicit Executor allowlist."""

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from langchain_core.tools import BaseTool, StructuredTool, ToolException
from mcp import Client
from mcp.types import CallToolResult, ImageContent, TextContent
from pydantic import BaseModel, ConfigDict, Field

from executor_test_agent.code_policy import PlannedStep
from executor_test_agent.config import AgentSettings
from executor_test_agent.integrations.contracts import AgentExecutionRequest

READ_TOOL_NAMES = frozenset(
    {
        "runtime_target_list",
        "runtime_target_get",
        "execution_list",
        "execution_get",
        "execution_step_list",
        "execution_operation_list",
        "execution_operation_get",
        "execution_operation_step_list",
        "execution_attempt_list",
        "execution_attempt_get",
        "execution_attempt_step_list",
        "execution_event_list",
        "execution_artifact_list",
        "execution_artifact_get",
        "execution_notebook_read",
        "execution_notebook_cell_read",
    }
)

MUTATION_MCP_TOOL_NAMES = frozenset(
    {
        "execution_submit",
        "execution_cancel",
        "execution_retry",
        "execution_operation_create",
        "execution_finalize",
    }
)

ADMIN_TOOL_NAMES = frozenset(
    {
        "runtime_target_upsert",
        "runtime_target_probe",
        "runtime_target_disable",
        "runtime_target_set_state",
    }
)

MCP_AGENT_SYSTEM_PROMPT = """
You are an Executor operations Agent. Use the supplied Tools to answer factual questions about
Runtime targets, supported profiles, capacities, Executions, Steps, Operations, Attempts, events,
Artifacts, and Runtime-owned notebooks. Never invent current state when a Tool can retrieve it.

Runtime supported_profiles are the selectable Jupyter kernel profiles. For a question such as
"which kernels are available", call runtime_target_list and summarize enabled ACTIVE targets and
their supported_profiles. Use opaque next_cursor values unchanged when another page is required.

You may submit, cancel, retry, append an Operation, or finalize an Execution only when the user
explicitly asks for that mutation. The mutation Tools enforce Agent-side identity, ownership,
code, version, and idempotency policies. Never claim that an asynchronous submission is complete
merely because its
Tool call returned. The application will wait for the Executor Redis event and reconcile the
authoritative result when a mutation returns wait_for_event=true.
""".strip()


class SubmitExecutionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtime_profile: str = Field(default="basic", min_length=1, max_length=128)
    steps: list[PlannedStep] = Field(min_length=1, max_length=5)
    operation_mode: str = Field(default="SINGLE", pattern=r"^(SINGLE|MULTI)$")


class OwnedExecutionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_id: str = Field(min_length=1)


class CancelExecutionInput(OwnedExecutionInput):
    reason: str | None = Field(default=None, max_length=2000)


class CreateOperationInput(OwnedExecutionInput):
    steps: list[PlannedStep] = Field(min_length=1, max_length=5)


def render_tool_result(result: CallToolResult) -> str:
    """Preserve structured MCP output and readable text/image metadata for the LLM."""
    if result.is_error:
        raise RuntimeError(_content_text(result) or "Executor MCP Tool failed.")
    if result.structured_content is not None:
        return json.dumps(result.structured_content, ensure_ascii=False, default=str)
    return _content_text(result)


async def load_executor_tools(
    settings: AgentSettings, *, request_scope_id: str | None = None
) -> list[BaseTool]:
    """Discover server schemas and return only Agent-approved read plus policy Tools."""
    async with Client(settings.executor_mcp_url) as client:
        discovered = await client.list_tools()
    by_name = {tool.name: tool for tool in discovered.tools}
    missing = READ_TOOL_NAMES - by_name.keys()
    if missing:
        raise RuntimeError(f"Executor MCP is missing required read Tools: {sorted(missing)}")
    leaked_admin = ADMIN_TOOL_NAMES & READ_TOOL_NAMES
    if leaked_admin:
        raise RuntimeError(f"Runtime admin Tools cannot be exposed: {sorted(leaked_admin)}")

    read_tools = [_read_tool(by_name[name], settings) for name in sorted(READ_TOOL_NAMES)]
    return [*read_tools, *_mutation_tools(settings, request_scope_id or uuid4().hex)]


def _read_tool(definition: Any, settings: AgentSettings) -> StructuredTool:
    async def invoke(**arguments: Any) -> str:
        try:
            sanitized = await _enforce_read_scope(definition.name, arguments, settings)
            return await _call_mcp(settings, definition.name, sanitized)
        except Exception as exc:
            raise ToolException(str(exc)) from exc

    return StructuredTool.from_function(
        coroutine=invoke,
        name=definition.name,
        description=definition.description or f"Call Executor MCP Tool {definition.name}.",
        args_schema=definition.input_schema,
        handle_tool_error=True,
    )


def _mutation_tools(settings: AgentSettings, request_scope_id: str) -> list[BaseTool]:
    return [
        _policy_tool(
            "execution_submit",
            "Submit one validated asynchronous execution. Use only for an explicit user request "
            "to actually run Python. Returns execution_id and wait_for_event=true.",
            SubmitExecutionInput,
            lambda arguments: _submit(arguments, settings, request_scope_id),
        ),
        _policy_tool(
            "execution_cancel",
            "Cancel an Execution owned by the configured test user.",
            CancelExecutionInput,
            lambda arguments: _cancel(arguments, settings, request_scope_id),
        ),
        _policy_tool(
            "execution_retry",
            "Retry a FAILED Execution owned by the configured test user.",
            OwnedExecutionInput,
            lambda arguments: _retry(arguments, settings, request_scope_id),
        ),
        _policy_tool(
            "execution_operation_create",
            "Append validated Steps to an owned MULTI Execution waiting for an Operation.",
            CreateOperationInput,
            lambda arguments: _create_operation(arguments, settings, request_scope_id),
        ),
        _policy_tool(
            "execution_finalize",
            "Finalize an owned MULTI Execution waiting for an Operation.",
            OwnedExecutionInput,
            lambda arguments: _finalize(arguments, settings, request_scope_id),
        ),
    ]


def _policy_tool(
    name: str,
    description: str,
    schema: type[BaseModel],
    handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
) -> StructuredTool:
    async def invoke(**arguments: Any) -> str:
        try:
            result = await handler(arguments)
        except Exception as exc:
            raise ToolException(str(exc)) from exc
        return json.dumps(result, ensure_ascii=False, default=str)

    return StructuredTool.from_function(
        coroutine=invoke,
        name=name,
        description=description,
        args_schema=schema,
        handle_tool_error=True,
    )


async def _submit(
    arguments: dict[str, Any], settings: AgentSettings, request_scope_id: str
) -> dict[str, Any]:
    parsed = SubmitExecutionInput.model_validate(arguments)
    idempotency_key = _idempotency_key("submit", request_scope_id, arguments)
    stable_id = hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]
    request = AgentExecutionRequest(
        runtime_profile=parsed.runtime_profile,
        user_id=settings.default_user_id,
        project_id=settings.default_project_id,
        session_id=f"tool-session-{stable_id}",
        task_id=f"tool-task-{stable_id}",
        steps=[step.model_dump(mode="json") for step in parsed.steps],
    )
    payload = request.executor_payload(idempotency_key, operation_mode=parsed.operation_mode)
    result = await _call_mcp_structured(settings, "execution_submit", {"request": payload})
    event_types = (
        ["execution.operation_succeeded", "execution.operation_failed"]
        if parsed.operation_mode == "MULTI"
        else ["execution.succeeded", "execution.failed", "execution.cancelled"]
    )
    return _mutation_result(result, wait_for_event=True, event_types=event_types)


async def _cancel(
    arguments: dict[str, Any], settings: AgentSettings, request_scope_id: str
) -> dict[str, Any]:
    parsed = CancelExecutionInput.model_validate(arguments)
    await _owned_execution(settings, parsed.execution_id)
    result = await _call_mcp_structured(
        settings,
        "execution_cancel",
        {
            "request": {
                "execution_id": parsed.execution_id,
                "idempotency_key": _idempotency_key("cancel", request_scope_id, arguments),
                "reason": parsed.reason,
                "actor": _actor(settings),
            }
        },
    )
    return _mutation_result(
        result,
        wait_for_event=True,
        event_types=["execution.cancelled", "execution.failed", "execution.succeeded"],
    )


async def _retry(
    arguments: dict[str, Any], settings: AgentSettings, request_scope_id: str
) -> dict[str, Any]:
    parsed = OwnedExecutionInput.model_validate(arguments)
    execution = await _owned_execution(settings, parsed.execution_id)
    if execution["state"]["status"] != "FAILED":
        raise ValueError("execution_retry requires an owned FAILED Execution.")
    result = await _call_mcp_structured(
        settings,
        "execution_retry",
        {
            "request": {
                "execution_id": parsed.execution_id,
                "idempotency_key": _idempotency_key("retry", request_scope_id, arguments),
                "actor": _actor(settings),
            }
        },
    )
    return _mutation_result(
        result,
        wait_for_event=True,
        event_types=["execution.succeeded", "execution.failed", "execution.cancelled"],
    )


async def _create_operation(
    arguments: dict[str, Any], settings: AgentSettings, request_scope_id: str
) -> dict[str, Any]:
    parsed = CreateOperationInput.model_validate(arguments)
    execution = await _owned_execution(settings, parsed.execution_id)
    if (
        execution["lifecycle"]["operation_mode"] != "MULTI"
        or execution["state"]["status"] != "WAITING_FOR_OPERATION"
    ):
        raise ValueError(
            "execution_operation_create requires an owned MULTI Execution waiting for an Operation."
        )
    steps_page = await _call_mcp_structured(
        settings, "execution_step_list", {"execution_id": parsed.execution_id, "limit": 200}
    )
    first_sequence = max((item["sequence"] for item in steps_page["items"]), default=-1) + 1
    source_steps = [
        {
            "sequence": first_sequence + offset,
            "payload": {"type": "CODE", "content": step.code},
            "lineage": {
                "skill_name": step.skill_name,
                "tool_name": step.tool_name,
                "input_parameters": {},
            },
        }
        for offset, step in enumerate(parsed.steps)
    ]
    result = await _call_mcp_structured(
        settings,
        "execution_operation_create",
        {
            "request": {
                "execution_id": parsed.execution_id,
                "idempotency_key": _idempotency_key(
                    "operation-create", request_scope_id, arguments
                ),
                "expected_version": execution["state"]["version"],
                "source": {
                    "type": "INLINE",
                    "spec": {
                        "schema_version": "1.0",
                        "steps": source_steps,
                    },
                },
                "actor": _actor(settings),
            }
        },
    )
    return _mutation_result(
        result,
        wait_for_event=True,
        event_types=["execution.operation_succeeded", "execution.operation_failed"],
    )


async def _finalize(
    arguments: dict[str, Any], settings: AgentSettings, request_scope_id: str
) -> dict[str, Any]:
    parsed = OwnedExecutionInput.model_validate(arguments)
    execution = await _owned_execution(settings, parsed.execution_id)
    if (
        execution["lifecycle"]["operation_mode"] != "MULTI"
        or execution["state"]["status"] != "WAITING_FOR_OPERATION"
    ):
        raise ValueError(
            "execution_finalize requires an owned MULTI Execution waiting for an Operation."
        )
    result = await _call_mcp_structured(
        settings,
        "execution_finalize",
        {
            "request": {
                "execution_id": parsed.execution_id,
                "idempotency_key": _idempotency_key("finalize", request_scope_id, arguments),
                "expected_version": execution["state"]["version"],
                "actor": _actor(settings),
            }
        },
    )
    return _mutation_result(
        result,
        wait_for_event=True,
        event_types=["execution.succeeded", "execution.failed", "execution.cancelled"],
    )


async def _owned_execution(settings: AgentSettings, execution_id: str) -> dict[str, Any]:
    execution = await _call_mcp_structured(
        settings, "execution_get", {"execution_id": execution_id}
    )
    if execution["context"]["user_id"] != settings.default_user_id:
        raise PermissionError("The configured Agent user does not own this Execution.")
    return execution


async def _enforce_read_scope(
    tool_name: str, arguments: dict[str, Any], settings: AgentSettings
) -> dict[str, Any]:
    sanitized = dict(arguments)
    if tool_name == "execution_list":
        requested_user = sanitized.get("user_id")
        if requested_user not in {None, settings.default_user_id}:
            raise PermissionError("The Agent cannot list another user's Executions.")
        sanitized["user_id"] = settings.default_user_id
    elif "execution_id" in sanitized:
        await _owned_execution(settings, str(sanitized["execution_id"]))
    elif tool_name == "execution_artifact_get":
        artifact = await _call_mcp_structured(settings, tool_name, sanitized)
        await _owned_execution(settings, str(artifact["produced_by"]["execution_id"]))
    return sanitized


async def _call_mcp(settings: AgentSettings, name: str, arguments: dict[str, Any]) -> str:
    async with Client(settings.executor_mcp_url) as client:
        result = await client.call_tool(name, arguments)
    return render_tool_result(result)


async def _call_mcp_structured(
    settings: AgentSettings, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    async with Client(settings.executor_mcp_url) as client:
        result = await client.call_tool(name, arguments)
    if result.is_error or not isinstance(result.structured_content, dict):
        raise RuntimeError(_content_text(result) or f"Executor MCP Tool {name} failed.")
    return result.structured_content


def _mutation_result(
    result: dict[str, Any], *, wait_for_event: bool, event_types: list[str]
) -> dict[str, Any]:
    return {
        "execution_id": result["execution_id"],
        "status": result["state"]["status"],
        "version": result["state"]["version"],
        "operation_id": result.get("operation_id"),
        "wait_for_event": wait_for_event,
        "event_types": event_types,
    }


def _actor(settings: AgentSettings) -> dict[str, str]:
    return {"type": "USER", "id": settings.default_user_id}


def _idempotency_key(prefix: str, request_scope_id: str, arguments: dict[str, Any]) -> str:
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:24]
    return f"agent-tool-{prefix}-{request_scope_id}-{digest}"


def _content_text(result: CallToolResult) -> str:
    parts: list[str] = []
    for item in result.content:
        if isinstance(item, TextContent):
            parts.append(item.text)
        elif isinstance(item, ImageContent):
            parts.append(f"[{item.mime_type}; base64]\n{item.data}")
    return "\n".join(parts)
