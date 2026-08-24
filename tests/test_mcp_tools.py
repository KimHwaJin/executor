import hashlib
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

from mcp import Client
from mcp.types import TextContent
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.services import ExecutionService
from executor_service.config import Settings
from executor_service.domain.enums import (
    ExecutionStatus,
    OperationStatus,
    RetryStrategy,
)
from executor_service.execution_specs import ExecutionSpecResolver
from executor_service.infrastructure.db.models import (
    ExecutionOperationORM,
    ExecutionORM,
)
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.execution_queries import (
    SQLAlchemyExecutionQueryService,
)
from executor_service.infrastructure.runtime_registry import (
    RuntimeTargetRegistry,
)
from executor_service.interfaces.mcp.server import build_mcp_server

SUBMIT_ARGUMENTS: dict[str, Any] = {
    "request": {
        "idempotency_key": "mcp-submit-1",
        "lifecycle": {"operation_mode": "SINGLE"},
        "trigger": {
            "type": "INTERACTIVE",
            "actor": {"type": "USER", "id": "user-1"},
        },
        "runtime": {"type": "JUPYTER", "profile": "basic"},
        "operation": {
            "spec": {
                "schema_version": "1.0",
                "steps": [
                    {
                        "sequence": 0,
                        "payload": {
                            "type": "PYTHON_EXECUTE",
                            "source": {
                                "type": "INLINE",
                                "content": "print('hello')",
                            },
                        },
                        "lineage": {
                            "skill_name": "data_load",
                            "tool_name": "load_data",
                            "input_parameters": {},
                        },
                    }
                ],
            },
        },
        "context": {
            "user_id": "user-1",
            "project_id": "project-1",
            "session_id": "session-1",
            "task_id": "task-1",
        },
    }
}


class _OutputContentService:
    async def describe(
        self,
        execution_id: UUID,
        output_id: UUID,
        representation_id: UUID,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            execution_id=execution_id,
            output_id=output_id,
            representation_id=representation_id,
            media_type="text/plain",
            size_bytes=12,
            checksum_sha256="a" * 64,
            complete=True,
        )

    async def read_inline_text(
        self, descriptor: Any, *, max_bytes: int
    ) -> str:
        del descriptor
        assert max_bytes == 1024
        return "complete text"


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
            "execution_submit",
            "execution_get",
            "execution_cancel",
            "execution_retry",
            "execution_operation_create",
            "execution_finalize",
        }

        submitted = await client.call_tool(
            "execution_submit", SUBMIT_ARGUMENTS
        )
        assert not submitted.is_error
        execution_id = submitted.structured_content["execution_id"]
        assert submitted.structured_content["state"]["status"] == "QUEUED"
        assert set(submitted.structured_content) == {
            "execution_id",
            "operation",
            "state",
            "created_by_type",
            "created_by",
            "updated_by_type",
            "updated_by",
            "created_at",
            "updated_at",
        }

        fetched = await client.call_tool(
            "execution_get", {"execution_id": execution_id}
        )
        assert not fetched.is_error
        assert fetched.structured_content["execution_id"] == execution_id
        assert fetched.structured_content["runtime"]["type"] == "JUPYTER"
        assert fetched.structured_content["context"]["user_id"] == "user-1"
        assert "steps" not in fetched.structured_content

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
        assert (
            cancelled.structured_content["state"]["status"]
            == "CANCEL_REQUESTED"
        )


async def test_mcp_output_content_inlines_small_text(
    execution_service: ExecutionService,
) -> None:
    target = build_mcp_server(
        execution_service,
        execution_queries=cast(Any, SimpleNamespace()),
        output_contents=cast(Any, _OutputContentService()),
        output_inline_max_bytes=1024,
    )
    execution_id = "11111111-1111-4111-8111-111111111111"
    output_id = "77777777-7777-4777-8777-777777777777"
    representation_id = "88888888-8888-4888-8888-888888888888"

    async with Client(target) as client:
        result = await client.call_tool(
            "execution_output_content_get",
            {
                "execution_id": execution_id,
                "output_id": output_id,
                "representation_id": representation_id,
            },
        )

    assert not result.is_error
    assert result.structured_content == {
        "execution_id": execution_id,
        "output_id": output_id,
        "representation_id": representation_id,
        "media_type": "text/plain",
        "size_bytes": 12,
        "checksum_sha256": "a" * 64,
        "complete": True,
        "delivery": "INLINE",
        "content": "complete text",
        "content_url": (
            f"/api/v1/executions/{execution_id}/outputs/{output_id}/"
            f"representations/{representation_id}/content"
        ),
    }


async def test_mcp_client_can_query_execution_history_resources(
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
            "execution_attempt_get",
            "execution_attempt_list",
            "execution_attempt_step_list",
            "execution_operation_get",
            "execution_operation_list",
            "execution_operation_step_list",
            "execution_step_list",
            "execution_artifact_get",
            "execution_artifact_list",
            "execution_event_list",
            "execution_output_get",
            "execution_output_list",
        }.issubset(tool_names)

        submitted = await client.call_tool(
            "execution_submit", SUBMIT_ARGUMENTS
        )
        execution_id = submitted.structured_content["execution_id"]
        operation_id = submitted.structured_content["operation"][
            "operation_id"
        ]
        attempts = await client.call_tool(
            "execution_attempt_list", {"execution_id": execution_id}
        )
        events = await client.call_tool(
            "execution_event_list", {"execution_id": execution_id}
        )
        operations = await client.call_tool(
            "execution_operation_list", {"execution_id": execution_id}
        )
        operation = await client.call_tool(
            "execution_operation_get",
            {"execution_id": execution_id, "operation_id": operation_id},
        )
        operation_steps = await client.call_tool(
            "execution_operation_step_list",
            {"execution_id": execution_id, "operation_id": operation_id},
        )

        assert attempts.structured_content["items"] == []
        assert (
            events.structured_content["items"][0]["event_type"]
            == "execution.submitted"
        )
        assert (
            operations.structured_content["items"][0]["operation_id"]
            == operation_id
        )
        assert operation.structured_content["sequence_range"] == {
            "first": 0,
            "last": 0,
        }
        assert operation_steps.structured_content["items"][0]["sequence"] == 0


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
            submitted = await client.call_tool("execution_submit", arguments)
            submitted_ids.add(submitted.structured_content["execution_id"])

        first = await client.call_tool("execution_list", {"limit": 1})
        assert not first.is_error
        assert len(first.structured_content["items"]) == 1
        assert "runtime" not in first.structured_content["items"][0]
        cursor = first.structured_content["next_cursor"]
        assert cursor

        second = await client.call_tool(
            "execution_list", {"limit": 1, "cursor": cursor}
        )
        assert not second.is_error
        returned_ids = {
            first.structured_content["items"][0]["execution_id"],
            second.structured_content["items"][0]["execution_id"],
        }
        assert returned_ids == submitted_ids
        assert second.structured_content["next_cursor"] is None


async def test_mcp_list_rejects_limit_outside_declared_contract(
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
        too_small = await client.call_tool("execution_list", {"limit": 0})
        too_large = await client.call_tool(
            "execution_event_list", {"limit": 501}
        )

    assert too_small.is_error
    assert too_large.is_error


async def test_mcp_domain_errors_expose_stable_public_code(
    execution_service: ExecutionService,
    tmp_path: Path,
) -> None:
    target = build_mcp_server(
        execution_service,
        execution_spec_resolver=ExecutionSpecResolver(tmp_path),
    )

    async with Client(target) as client:
        missing = await client.call_tool(
            "execution_get",
            {"execution_id": "00000000-0000-0000-0000-000000000001"},
        )

    assert missing.is_error
    assert isinstance(missing.content[0], TextContent)
    assert "[EXECUTION_NOT_FOUND]" in missing.content[0].text


async def test_mcp_runtime_target_disable_uses_shared_contract(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    registry = RuntimeTargetRegistry(
        create_session_factory(engine),
        Settings(
            runtime_enabled=False,
            jupyter_request_timeout_seconds=0.1,
            runtime_credential_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        ),
    )
    target = build_mcp_server(
        execution_service,
        runtime_manager=registry,
        execution_spec_resolver=ExecutionSpecResolver(tmp_path),
    )

    async with Client(target) as client:
        tool_names = {tool.name for tool in (await client.list_tools()).tools}
        assert "runtime_target_disable" in tool_names
        created = await client.call_tool(
            "runtime_target_upsert",
            {
                "request": {
                    "idempotency_key": "mcp-target-upsert-1",
                    "name": "mcp-target",
                    "runtime_type": "JUPYTER",
                    "connection_config": {"endpoint": "http://127.0.0.1:9"},
                    "credential": "not-returned",
                    "pool": "INTERACTIVE",
                    "actor": {"type": "USER", "id": "operator-1"},
                }
            },
        )
        disabled = await client.call_tool(
            "runtime_target_disable",
            {
                "request": {
                    "target_id": created.structured_content["target_id"],
                    "idempotency_key": "mcp-target-disable-1",
                    "actor": {"type": "USER", "id": "operator-1"},
                }
            },
        )

    assert not disabled.is_error
    assert disabled.structured_content["runtime"]["connection_config"] == {
        "endpoint": "http://127.0.0.1:9"
    }
    assert disabled.structured_content["state"] == {
        "status": "OFFLINE",
        "enabled": False,
        "accepting_new_executions": False,
        "drain_complete": False,
    }
    assert disabled.structured_content["capacity"] == {
        "max_concurrent_executions": 2,
        "active_execution_count": 0,
        "active_session_count": None,
        "admission_used_count": 0,
        "available_capacity": 0,
        "admission_blocked": False,
        "session_count_observed_at": None,
        "session_count_fresh": False,
    }
    assert "credential" not in disabled.structured_content


async def test_execution_submit_reads_path_spec_and_derives_batch_pool(
    execution_service: ExecutionService,
    tmp_path: Path,
) -> None:
    content = b"print('hello from PATH')"
    source_file = tmp_path / "plans" / "batch-step.py"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(content)
    arguments = deepcopy(SUBMIT_ARGUMENTS)
    arguments["request"]["idempotency_key"] = "mcp-path-submit-1"
    arguments["request"]["trigger"] = {
        "type": "BATCH",
        "actor": {"type": "BATCH", "id": "batch-1"},
    }
    arguments["request"]["context"]["workflow_id"] = "workflow-batch-1"
    arguments["request"]["operation"]["spec"]["steps"][0]["payload"] = {
        "type": "PYTHON_EXECUTE",
        "source": {
            "type": "PATH",
            "path": "plans/batch-step.py",
            "sha256": hashlib.sha256(content).hexdigest(),
        },
    }
    target = build_mcp_server(
        execution_service,
        execution_spec_resolver=ExecutionSpecResolver(tmp_path),
    )

    async with Client(target) as client:
        submitted = await client.call_tool("execution_submit", arguments)

    assert not submitted.is_error
    assert submitted.structured_content["created_by"] == "batch-1"
    execution = await execution_service.get(
        UUID(submitted.structured_content["execution_id"])
    )
    assert execution.runtime_pool.value == "BATCH"
    assert execution.steps[0].source_path == "plans/batch-step.py"
    assert execution.user_id == "user-1"


async def test_multi_continue_accepts_next_inline_execution_spec(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    arguments = deepcopy(SUBMIT_ARGUMENTS)
    arguments["request"]["idempotency_key"] = "mcp-multi-submit-1"
    arguments["request"]["lifecycle"] = {
        "operation_mode": "MULTI",
        "operation_wait_timeout_seconds": 600,
    }
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
                .values(status="WAITING_FOR_OPERATION", version=2)
            )
        continued = await client.call_tool(
            "execution_operation_create",
            {
                "request": {
                    "execution_id": execution_id,
                    "idempotency_key": "mcp-multi-continue-1",
                    "expected_version": 2,
                    "actor": {"type": "USER", "id": "user-1"},
                    "spec": {
                        "schema_version": "1.0",
                        "steps": [
                            {
                                "sequence": 1,
                                "payload": {
                                    "type": "PYTHON_EXECUTE",
                                    "source": {
                                        "type": "INLINE",
                                        "content": "print('next')",
                                    },
                                },
                            },
                            {
                                "sequence": 2,
                                "payload": {
                                    "type": "PYTHON_EXECUTE",
                                    "source": {
                                        "type": "INLINE",
                                        "content": "print('next again')",
                                    },
                                },
                            },
                        ],
                    },
                }
            },
        )

    assert not continued.is_error
    execution = await execution_service.get(UUID(execution_id))
    assert [step.sequence for step in execution.steps] == [0, 1, 2]
    assert execution.steps[1].operation_id == execution.steps[2].operation_id


async def test_mcp_retry_returns_the_requeued_operation_id(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    arguments = deepcopy(SUBMIT_ARGUMENTS)
    arguments["request"]["idempotency_key"] = "mcp-retry-operation-submit"
    target = build_mcp_server(
        execution_service,
        execution_spec_resolver=ExecutionSpecResolver(tmp_path),
    )

    async with Client(target) as client:
        submitted = await client.call_tool("execution_submit", arguments)
        execution_id = UUID(submitted.structured_content["execution_id"])
        operation_id = UUID(
            submitted.structured_content["operation"]["operation_id"]
        )
        session_factory = create_session_factory(engine)
        async with session_factory() as session, session.begin():
            await session.execute(
                update(ExecutionORM)
                .where(ExecutionORM.id == execution_id)
                .values(
                    status=ExecutionStatus.FAILED,
                    retry_strategy=RetryStrategy.FROM_START,
                    retry_from_sequence=0,
                )
            )
            await session.execute(
                update(ExecutionOperationORM)
                .where(ExecutionOperationORM.id == operation_id)
                .values(status=OperationStatus.FAILED)
            )
        retried = await client.call_tool(
            "execution_retry",
            {
                "request": {
                    "execution_id": str(execution_id),
                    "idempotency_key": "mcp-retry-operation-command",
                    "actor": {"type": "USER", "id": "user-1"},
                }
            },
        )

    assert not retried.is_error
    assert retried.structured_content["operation"]["operation_id"] == str(
        operation_id
    )
    assert retried.structured_content["state"]["status"] == "QUEUED"
