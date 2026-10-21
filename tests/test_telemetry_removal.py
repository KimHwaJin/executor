"""Execution delivery and failure evidence do not require telemetry."""

import ast
import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from executor_service.domain.runtime import RuntimeDriverError
from executor_service.infrastructure.db.models import (
    ExecutionEventORM,
    ExecutionORM,
    OutboxEventORM,
)
from executor_service.infrastructure.execution_worker.runtime_calls import (
    run_runtime_operation,
)
from executor_service.settings import Settings


def test_executor_has_no_opentelemetry_imports_or_settings() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "executor_service"
    assert not (root / "tracing.py").exists()
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("opentelemetry")
            if isinstance(node, ast.Import):
                assert all(
                    not alias.name.startswith("opentelemetry")
                    for alias in node.names
                )
    assert "tracing_enabled" not in Settings.model_fields
    assert not any(name.startswith("otel_") for name in Settings.model_fields)


@pytest.mark.parametrize(
    "model", [ExecutionORM, ExecutionEventORM, OutboxEventORM]
)
def test_schema_and_queries_have_no_trace_fields(
    model: type[ExecutionORM] | type[ExecutionEventORM] | type[OutboxEventORM],
) -> None:
    assert not {"traceparent", "tracestate"} & set(
        model.__table__.columns.keys()
    )
    statement = str(select(model))
    assert "traceparent" not in statement
    assert "tracestate" not in statement


async def test_runtime_wrapper_preserves_safe_logs_and_original_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    execution_id, target_id = uuid4(), uuid4()
    error = RuntimeDriverError("Jupyter REST request failed: status=500.")

    async def fail() -> None:
        try:
            raise ValueError("password=secret-value")
        except ValueError as cause:
            raise error from cause

    with pytest.raises(RuntimeDriverError) as raised:
        await run_runtime_operation(
            "executor.runtime.code.execute",
            fail(),
            execution_id=execution_id,
            target_id=target_id,
            sequence=2,
        )
    assert raised.value is error
    record = json.loads(caplog.records[-1].getMessage())
    assert record["execution_id"] == str(execution_id)
    assert record["phase"] == "executor.runtime.code.execute"
    assert "status=500" in caplog.text
    assert "secret-value" not in caplog.text


async def test_runtime_wrapper_preserves_result_and_cancellation() -> None:
    async def succeed() -> str:
        return "result"

    async def cancelled() -> None:
        raise asyncio.CancelledError

    execution_id, target_id = uuid4(), uuid4()
    assert (
        await run_runtime_operation(
            "execute",
            succeed(),
            execution_id=execution_id,
            target_id=target_id,
        )
        == "result"
    )
    with pytest.raises(asyncio.CancelledError):
        await run_runtime_operation(
            "execute",
            cancelled(),
            execution_id=execution_id,
            target_id=target_id,
        )
