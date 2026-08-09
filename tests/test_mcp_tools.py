from mcp import Client
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.services import ExecutionService
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.execution_queries import SQLAlchemyExecutionQueryService
from executor_service.interfaces.mcp.server import build_mcp_server

SUBMIT_ARGUMENTS = {
    "request": {
        "idempotency_key": "mcp-submit-1",
        "mode": "STATIC",
        "trigger_type": "INTERACTIVE",
        "jupyter_pool": "INTERACTIVE",
        "kernel_name": "python-analysis-a",
        "source": {"type": "INLINE", "code": "print('hello')"},
        "context": {
            "requested_by_user_id": "user-1",
            "project_id": "project-1",
            "session_id": "session-1",
            "execution_plan_id": "plan-1",
        },
        "steps": [
            {
                "sequence": 0,
                "skill_name": "data_load",
                "tool_name": "load_data",
                "input_parameters": {},
            }
        ],
    }
}


async def test_mcp_client_can_list_and_call_execution_tools(
    execution_service: ExecutionService,
) -> None:
    server = build_mcp_server(execution_service)

    async with Client(server) as client:
        listed = await client.list_tools()
        tool_names = {tool.name for tool in listed.tools}
        assert tool_names == {
            "executor_get_capabilities",
            "execution_submit",
            "execution_get",
            "execution_cancel",
            "execution_retry",
            "execution_continue",
            "execution_finish",
        }

        capabilities = await client.call_tool("executor_get_capabilities")
        assert not capabilities.is_error
        assert capabilities.structured_content["protocol_revision"] == "2026-07-28"
        assert "WORKER_SHUTDOWN" in capabilities.structured_content["failure_types"]
        assert capabilities.structured_content["retry_strategies"] == [
            "NOT_RETRYABLE",
            "FROM_FAILED_STEP",
            "FROM_START",
        ]
        assert capabilities.structured_content["implemented_execution_modes"] == [
            "STATIC",
            "DYNAMIC",
        ]

        submitted = await client.call_tool("execution_submit", SUBMIT_ARGUMENTS)
        assert not submitted.is_error
        execution_id = submitted.structured_content["execution_id"]
        assert submitted.structured_content["status"] == "QUEUED"

        fetched = await client.call_tool("execution_get", {"execution_id": execution_id})
        assert not fetched.is_error
        assert fetched.structured_content["execution_id"] == execution_id

        cancelled = await client.call_tool(
            "execution_cancel",
            {
                "request": {
                    "execution_id": execution_id,
                    "idempotency_key": "mcp-cancel-1",
                    "reason": "integration test",
                }
            },
        )
        assert not cancelled.is_error
        assert cancelled.structured_content["status"] == "CANCEL_REQUESTED"


async def test_mcp_client_can_query_execution_trace(
    execution_service: ExecutionService,
    engine: AsyncEngine,
) -> None:
    queries = SQLAlchemyExecutionQueryService(create_session_factory(engine))
    server = build_mcp_server(execution_service, execution_queries=queries)

    async with Client(server) as client:
        listed = await client.list_tools()
        tool_names = {tool.name for tool in listed.tools}
        assert {
            "execution_attempt_list",
            "execution_artifact_get",
            "execution_artifact_list",
            "execution_event_list",
            "execution_trace_get",
        }.issubset(tool_names)

        submitted = await client.call_tool("execution_submit", SUBMIT_ARGUMENTS)
        execution_id = submitted.structured_content["execution_id"]
        trace = await client.call_tool(
            "execution_trace_get", {"execution_id": execution_id}
        )

        assert not trace.is_error
        assert trace.structured_content["execution"]["execution_id"] == execution_id
        assert trace.structured_content["attempts"] == []
        assert trace.structured_content["events"][0]["event_type"] == "execution.submitted"
