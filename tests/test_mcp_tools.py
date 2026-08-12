import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import UUID

from mcp import Client
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.services import ExecutionService
from executor_service.infrastructure.db.models import ExecutionORM
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.execution_queries import SQLAlchemyExecutionQueryService
from executor_service.interfaces.mcp.execution_specs import ExecutionSpecResolver
from executor_service.interfaces.mcp.server import build_mcp_server

SUBMIT_ARGUMENTS: dict[str, Any] = {
    "request": {
        "idempotency_key": "mcp-submit-1",
        "mode": "STATIC",
        "trigger_type": "INTERACTIVE",
        "actor": {"type": "USER", "id": "user-1"},
        "runtime_profile": "python-analysis-a",
        "source": {
            "type": "INLINE",
            "spec": {
                "schema_version": "1.0",
                "execution_plan_id": "plan-1",
                "steps": [
                    {
                        "sequence": 0,
                        "plan_step_id": "plan-step-1",
                        "skill_name": "data_load",
                        "tool_name": "load_data",
                        "input_parameters": {},
                        "code": "print('hello')",
                    }
                ],
            },
        },
        "context": {
            "requested_by_user_id": "user-1",
            "project_id": "project-1",
            "session_id": "session-1",
            "task_id": "task-1",
        },
    }
}


async def test_mcp_client_can_list_and_call_execution_tools(
    execution_service: ExecutionService,
    tmp_path: Path,
) -> None:
    target = build_mcp_server(
        execution_service,
        execution_spec_resolver=ExecutionSpecResolver(tmp_path),
    )

    async with Client(target) as client:
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
        assert submitted.structured_content["runtime_pool"] == "INTERACTIVE"
        assert submitted.structured_content["source"]["type"] == "INLINE"
        assert len(submitted.structured_content["source"]["sha256"]) == 64
        assert submitted.structured_content["context"]["task_id"] == "task-1"
        assert submitted.structured_content["steps"][0]["plan_step_id"] == "plan-step-1"

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
                    "actor": {"type": "USER", "id": "user-1"},
                }
            },
        )
        assert not cancelled.is_error
        assert cancelled.structured_content["status"] == "CANCEL_REQUESTED"


async def test_mcp_client_can_query_execution_trace(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    queries = SQLAlchemyExecutionQueryService(create_session_factory(engine))
    target = build_mcp_server(
        execution_service,
        execution_queries=queries,
        execution_spec_resolver=ExecutionSpecResolver(tmp_path),
    )

    async with Client(target) as client:
        listed = await client.list_tools()
        tool_names = {tool.name for tool in listed.tools}
        assert {
            "execution_attempt_list",
            "execution_step_list",
            "execution_artifact_get",
            "execution_artifact_list",
            "execution_event_list",
            "execution_trace_get",
        }.issubset(tool_names)

        submitted = await client.call_tool("execution_submit", SUBMIT_ARGUMENTS)
        execution_id = submitted.structured_content["execution_id"]
        trace = await client.call_tool("execution_trace_get", {"execution_id": execution_id})

        assert not trace.is_error
        assert trace.structured_content["execution"]["execution_id"] == execution_id
        assert trace.structured_content["attempts"]["items"] == []
        assert trace.structured_content["events"]["items"][0]["event_type"] == "execution.submitted"


async def test_mcp_execution_list_uses_opaque_next_cursor(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    queries = SQLAlchemyExecutionQueryService(create_session_factory(engine))
    target = build_mcp_server(
        execution_service,
        execution_queries=queries,
        execution_spec_resolver=ExecutionSpecResolver(tmp_path),
    )
    arguments = deepcopy(SUBMIT_ARGUMENTS)

    async with Client(target) as client:
        submitted_ids: set[str] = set()
        for index in range(2):
            arguments["request"]["idempotency_key"] = f"mcp-page-{index}"
            arguments["request"]["source"]["spec"]["execution_plan_id"] = f"mcp-page-plan-{index}"
            submitted = await client.call_tool("execution_submit", arguments)
            submitted_ids.add(submitted.structured_content["execution_id"])

        first = await client.call_tool("execution_list", {"limit": 1})
        assert not first.is_error
        assert len(first.structured_content["items"]) == 1
        cursor = first.structured_content["nextCursor"]
        assert cursor

        second = await client.call_tool("execution_list", {"limit": 1, "cursor": cursor})
        assert not second.is_error
        returned_ids = {
            first.structured_content["items"][0]["execution_id"],
            second.structured_content["items"][0]["execution_id"],
        }
        assert returned_ids == submitted_ids
        assert second.structured_content["nextCursor"] is None


async def test_execution_submit_reads_path_spec_and_derives_batch_pool(
    execution_service: ExecutionService,
    tmp_path: Path,
) -> None:
    spec = deepcopy(SUBMIT_ARGUMENTS["request"]["source"]["spec"])
    spec["execution_plan_id"] = "batch-plan-1"
    content = json.dumps(spec, separators=(",", ":")).encode()
    source_file = tmp_path / "plans" / "batch.execution.json"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(content)
    arguments = deepcopy(SUBMIT_ARGUMENTS)
    arguments["request"]["idempotency_key"] = "mcp-path-submit-1"
    arguments["request"]["trigger_type"] = "BATCH"
    arguments["request"]["actor"] = {"type": "BATCH", "id": "batch-1"}
    arguments["request"]["source"] = {
        "type": "PATH",
        "path": "plans/batch.execution.json",
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    target = build_mcp_server(
        execution_service,
        execution_spec_resolver=ExecutionSpecResolver(tmp_path),
    )

    async with Client(target) as client:
        submitted = await client.call_tool("execution_submit", arguments)

    assert not submitted.is_error
    assert submitted.structured_content["runtime_pool"] == "BATCH"
    assert submitted.structured_content["source"]["path"] == "plans/batch.execution.json"
    assert submitted.structured_content["context"]["execution_plan_id"] == "batch-plan-1"


async def test_dynamic_continue_accepts_next_inline_execution_spec(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    arguments = deepcopy(SUBMIT_ARGUMENTS)
    arguments["request"]["idempotency_key"] = "mcp-dynamic-submit-1"
    arguments["request"]["mode"] = "DYNAMIC"
    target = build_mcp_server(
        execution_service,
        execution_spec_resolver=ExecutionSpecResolver(tmp_path),
    )

    async with Client(target) as client:
        submitted = await client.call_tool("execution_submit", arguments)
        execution_id = submitted.structured_content["execution_id"]
        session_factory = create_session_factory(engine)
        async with session_factory() as session, session.begin():
            await session.execute(
                update(ExecutionORM)
                .where(ExecutionORM.id == UUID(execution_id))
                .values(status="WAITING_FOR_NEXT_STEP", version=2)
            )
        continued = await client.call_tool(
            "execution_continue",
            {
                "request": {
                    "execution_id": execution_id,
                    "idempotency_key": "mcp-dynamic-continue-1",
                    "expected_version": 2,
                    "actor": {"type": "USER", "id": "user-1"},
                    "source": {
                        "type": "INLINE",
                        "spec": {
                            "schema_version": "1.0",
                            "execution_plan_id": "plan-2",
                            "steps": [
                                {
                                    "sequence": 1,
                                    "plan_step_id": "plan-2-step-1",
                                    "code": "print('next')",
                                }
                            ],
                        },
                    },
                }
            },
        )

    assert not continued.is_error
    assert continued.structured_content["steps"][1]["execution_plan_id"] == "plan-2"
    assert continued.structured_content["steps"][1]["plan_step_id"] == "plan-2-step-1"
