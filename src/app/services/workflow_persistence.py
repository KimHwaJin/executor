"""Per-execution snapshots and version references, separate from reusable Workflows."""

from __future__ import annotations

from copy import deepcopy
import os
from typing import Any, Protocol
from uuid import UUID

from app.services.async_io_resources import workflow_connection_pool
from psycopg.types.json import Jsonb


class WorkflowStore(Protocol):
    async def start_execution(
        self,
        *,
        execution_id: str,
        version_id: str | None = None,
        execution_inputs: list[dict[str, Any]] | None = None,
        session_id: str,
        task_id: str,
        revision: int,
        workflow: dict[str, Any],
        status: str,
    ) -> None: ...

    async def record_adaptive_round(
        self,
        *,
        execution_id: str,
        decision_round: int,
        changes: list[dict[str, Any]],
        effective_workflow: dict[str, Any],
    ) -> None: ...

    async def finish_execution(
        self,
        *,
        execution_id: str,
        status: str,
        final_workflow: dict[str, Any],
        successful: bool | None = None,
    ) -> None: ...


class NullWorkflowStore:
    """No-op store used by unit tests and callers without application storage."""

    async def start_execution(self, **_: Any) -> None:
        return None

    async def record_adaptive_round(self, **_: Any) -> None:
        return None

    async def finish_execution(self, **_: Any) -> None:
        return None


def reusable_workflow_snapshot(workflow: dict[str, Any]) -> dict[str, Any]:
    """Remove execution-bound values while retaining the complete definition."""

    document = deepcopy(workflow)
    definition = document.get("workflow")
    if not isinstance(definition, dict):
        return document

    context = definition.get("context")
    if isinstance(context, dict):
        context.pop("output_dir", None)

    input_schema = definition.get("input_schema")
    if isinstance(input_schema, dict):
        existing_unresolved = {
            str(item.get("name")): item
            for item in definition.get("unresolved_inputs") or []
            if isinstance(item, dict) and item.get("name")
        }
        definition["inputs"] = {}
        definition["input_provenance"] = {}
        definition["unresolved_inputs"] = [
            {
                "name": name,
                "question": str(
                    existing_unresolved.get(name, {}).get("question")
                    or f"{name} 값을 입력해주세요."
                ),
                "required_for": list(
                    existing_unresolved.get(name, {}).get("required_for") or []
                ),
            }
            for name, schema in input_schema.items()
            if not isinstance(schema, dict) or schema.get("required", True)
        ]
        if definition["unresolved_inputs"]:
            definition["status"] = "needs_input"
    return document


def effective_workflow_snapshot(
    workflow: dict[str, Any], runtime_decisions: dict[str, str]
) -> dict[str, Any]:
    """Materialize runtime decisions without overwriting declared execution rules."""

    document = deepcopy(workflow)
    definition = document.get("workflow")
    if not isinstance(definition, dict):
        return document
    for step in definition.get("steps") or []:
        if not isinstance(step, dict):
            continue
        for tool in step.get("tools") or []:
            if not isinstance(tool, dict):
                continue
            tool_id = str(tool.get("id") or "")
            if tool_id in runtime_decisions:
                tool["runtime_decision"] = runtime_decisions[tool_id]
    return document


class PostgresWorkflowStore:
    """Async repository; connections and transactions are owned by a shared pool."""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url.replace(
            "postgresql+psycopg://", "postgresql://", 1
        )

    @staticmethod
    async def _validate_version(conn, version_id, workflow_id, revision):
        if version_id is None:
            return
        cursor = await conn.execute("SELECT workflow_id,revision FROM workflow_version WHERE version_id=%s", (UUID(str(version_id)),))
        row = await cursor.fetchone()
        if row is None or str(row[0]) != workflow_id or row[1] != revision:
            raise ValueError("Execution workflow ID/revision does not match the selected Workflow version")

    async def start_execution(self, *, execution_id: str,
                              session_id: str, task_id: str, revision: int,
                              workflow: dict[str, Any], status: str, version_id: str | None = None,
                              execution_inputs: list[dict[str, Any]] | None = None) -> None:
        workflow_id = str((workflow.get("workflow") or {}).get("id") or "")
        if not execution_id or not workflow_id:
            return
        if execution_inputs is not None and not isinstance(execution_inputs, list):
            raise ValueError("execution_inputs must be an array")
        pool = await workflow_connection_pool(self.database_url)
        async with pool.connection() as conn:
            await self._validate_version(conn, version_id, workflow_id, revision)
            cursor = await conn.execute("""
                INSERT INTO workflow_executions
                (execution_id,version_id,workflow_id,workflow_revision,
                 session_id,task_id,original_workflow,execution_inputs,execution_status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (execution_id) DO UPDATE SET
                    execution_status=CASE WHEN workflow_executions.finished_at IS NULL
                        THEN EXCLUDED.execution_status ELSE workflow_executions.execution_status END,
                    updated_at=now()
                WHERE workflow_executions.version_id IS NOT DISTINCT FROM EXCLUDED.version_id
                  AND workflow_executions.workflow_id=EXCLUDED.workflow_id
                  AND workflow_executions.workflow_revision=EXCLUDED.workflow_revision
                  AND workflow_executions.session_id=EXCLUDED.session_id
                  AND workflow_executions.task_id=EXCLUDED.task_id
                RETURNING execution_id
            """, (UUID(execution_id),
                  UUID(str(version_id)) if version_id else None, workflow_id, revision, session_id, task_id,
                  Jsonb(workflow), Jsonb(execution_inputs or []), status or "submitted"))
            if await cursor.fetchone() is None:
                raise ValueError("Execution ID is already associated with another Workflow version or task")

    async def record_adaptive_round(
        self,
        *,
        execution_id: str,
        decision_round: int,
        changes: list[dict[str, Any]],
        effective_workflow: dict[str, Any],
    ) -> None:
        if not execution_id:
            return
        pool = await workflow_connection_pool(self.database_url)
        async with pool.connection() as conn:
            await conn.execute(
                """INSERT INTO workflow_adaptive_history
                (execution_id, decision_round, changes, effective_workflow)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT (execution_id, decision_round) DO UPDATE SET
                    changes=EXCLUDED.changes,
                    effective_workflow=EXCLUDED.effective_workflow
                """,
                (
                    UUID(execution_id),
                    decision_round,
                    Jsonb(changes),
                    Jsonb(effective_workflow),
                ),
            )

    async def finish_execution(
        self,
        *,
        execution_id: str,
        status: str,
        final_workflow: dict[str, Any],
        successful: bool | None = None,
    ) -> None:
        if not execution_id:
            return
        pool = await workflow_connection_pool(self.database_url)
        async with pool.connection() as conn:
            # First completion wins; retries cannot rewrite a finished execution.
            await conn.execute("""
                UPDATE workflow_executions SET final_workflow=%s, execution_status=%s, successful=%s,
                    finished_at=now(), updated_at=now()
                WHERE execution_id=%s AND finished_at IS NULL
            """, (Jsonb(final_workflow), status, status == "SUCCEEDED" if successful is None else successful, UUID(execution_id)))


def workflow_store_from_environment() -> WorkflowStore:
    """Build application persistence from the existing Worker database URL."""

    database_url = (
        os.environ.get("WORKFLOW_DATABASE_URL", "").strip()
        or os.environ.get("EW_DATABASE_URL", "").strip()
    )
    return PostgresWorkflowStore(database_url) if database_url else NullWorkflowStore()


__all__ = [
    "NullWorkflowStore",
    "PostgresWorkflowStore",
    "WorkflowStore",
    "effective_workflow_snapshot",
    "reusable_workflow_snapshot",
    "workflow_store_from_environment",
]
