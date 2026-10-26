"""HTTP client helpers for submitting execution requests to Executor."""

from __future__ import annotations

import json
import logging
from time import perf_counter
from typing import Any
import httpx
from urllib.parse import urlencode
from app.services.async_io_resources import executor_http_client

from agent_config import AgentSettings
from app.schemas.agents.orchestration_schema import ExecutorSubmitResponse

logger = logging.getLogger(__name__)


class ExecutorSubmitError(RuntimeError):
    """Raised when Executor does not accept an execution request."""


def _format_execution_url(path_template: str, execution_id: str) -> str:
    if not execution_id.strip():
        raise ExecutorSubmitError("execution_id must be a non-empty string")
    return path_template.format(execution_id=execution_id)


async def _post_json(
    url: str,
    payload: dict[str, Any],
    *,
    timeout_seconds: float,
    action: str = "post",
    execution_id: str | None = None,
) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    context = payload.get("context")
    task_id = context.get("task_id") if isinstance(context, dict) else None
    identifiers = (
        action, execution_id, task_id, payload.get("idempotency_key")
    )
    log_context = "action=%s execution_id=%s task_id=%s idempotency_key=%s"
    started_at = perf_counter()
    status_code = None
    logger.info("Executor request started " + log_context, *identifiers)
    try:
        response = await executor_http_client().post(
            url, content=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=timeout_seconds,
        )
        status_code = response.status_code
        response.raise_for_status()
        logger.info(
            "Executor response received " + log_context
            + " status_code=%s elapsed_ms=%.1f",
            *identifiers, status_code, (perf_counter() - started_at) * 1000,
        )
        parsed_body: Any = json.loads(response.content) if response.content else None
        return {"status_code": status_code, "body": parsed_body}
    except httpx.HTTPStatusError as exc:
        error_body = exc.response.text

        message = (
            f"Executor request failed "
            f"action={action} execution_id={execution_id} "
            f"task_id={task_id} "
            f"idempotency_key={payload.get('idempotency_key')} "
            f"status_code={exc.response.status_code} "
            f"elapsed_ms={(perf_counter() - started_at) * 1000:.1f} "
            f"request_body={body.decode('utf-8', errors='replace')} "
            f"response_body={error_body}"
        )

        logger.exception(message)
        raise ExecutorSubmitError(message) from exc
    except httpx.RequestError as exc:
        message = (
            f"Executor request failed "
            f"action={action} execution_id={execution_id} "
            f"task_id={task_id} "
            f"idempotency_key={payload.get('idempotency_key')} "
            f"elapsed_ms={(perf_counter() - started_at) * 1000:.1f} "
            f"reason={exc}"
        )

        logger.exception(message)
        raise ExecutorSubmitError(message) from exc
    except Exception as exc:
        message = (
            f"Executor request or response processing failed "
            f"action={action} execution_id={execution_id} "
            f"task_id={task_id} "
            f"idempotency_key={payload.get('idempotency_key')} "
            f"status_code={status_code} "
            f"elapsed_ms={(perf_counter() - started_at) * 1000:.1f} "
            f"error_type={type(exc).__name__} reason={exc}"
        )

        logger.exception(message)
        raise


async def _get_json(
    url: str,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    try:
        response = await executor_http_client().get(
            url, headers={"Accept": "application/json"}, timeout=timeout_seconds,
        )
        response.raise_for_status()
        parsed_body: Any = json.loads(response.content) if response.content else None
        if not isinstance(parsed_body, dict):
            raise ExecutorSubmitError("executor GET response must be a JSON object")
        return {"status_code": response.status_code, "body": parsed_body}
    except httpx.HTTPStatusError as exc:
        error_body = exc.response.text
        raise ExecutorSubmitError(
            f"executor request failed with HTTP {exc.response.status_code}: {error_body}"
        ) from exc
    except httpx.RequestError as exc:
        raise ExecutorSubmitError(f"executor request failed: {exc}") from exc


async def get_execution(
    settings: AgentSettings,
    execution_id: str,
) -> dict[str, Any]:
    return await _get_json(
        _format_execution_url(settings.executor_execution_url, execution_id),
        timeout_seconds=settings.executor_timeout_seconds,
    )


async def get_execution_result(
    settings: AgentSettings,
    execution_id: str,
) -> dict[str, Any]:
    return await _get_json(
        _format_execution_url(settings.executor_result_url, execution_id),
        timeout_seconds=settings.executor_timeout_seconds,
    )


async def get_execution_notebook(
    settings: AgentSettings,
    execution_id: str,
    *,
    view: str = "FULL",
    start_index: int = 0,
    limit: int = 200,
) -> dict[str, Any]:
    if view not in {"SUMMARY", "FULL"}:
        raise ValueError("notebook view must be SUMMARY or FULL")
    if start_index < 0:
        raise ValueError("start_index must be at least 0")
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    base_url = _format_execution_url(settings.executor_notebook_url, execution_id)
    query = urlencode(
        {"view": view, "start_index": start_index, "limit": limit}
    )
    return await _get_json(
        f"{base_url}?{query}",
        timeout_seconds=settings.executor_timeout_seconds,
    )


async def submit_execution_start(
    settings: AgentSettings,
    payload: dict[str, Any],
) -> dict[str, Any]:
    logger.debug("[DEBUG] Execution Submit Start")
    response = await _post_json(
        settings.executor_executions_url,
        payload,
        timeout_seconds=settings.executor_timeout_seconds,
        action="start",
    )
    if isinstance(response.get("body"), dict):
        ExecutorSubmitResponse.model_validate(response["body"])
    return response


async def submit_execution_continue(
    settings: AgentSettings,
    execution_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    logger.debug("[DEBUG] Execution Submit Continue")
    response = await _post_json(
        _format_execution_url(settings.executor_operations_url, execution_id),
        payload,
        timeout_seconds=settings.executor_timeout_seconds,
        action="continue",
        execution_id=execution_id,
    )
    if isinstance(response.get("body"), dict):
        ExecutorSubmitResponse.model_validate(response["body"])
    return response


async def submit_execution_finish(
    settings: AgentSettings,
    execution_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    logger.debug("[DEBUG] Execution Submit Finish")
    return await _post_json(
        _format_execution_url(settings.executor_finalize_url, execution_id),
        payload,
        timeout_seconds=settings.executor_timeout_seconds,
        action="finish",
        execution_id=execution_id,
    )


async def submit_execution_cancel(
    settings: AgentSettings,
    execution_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    logger.debug("[DEBUG] Execution Submit Cancel")
    return await _post_json(
        _format_execution_url(settings.executor_cancel_url, execution_id),
        payload,
        timeout_seconds=settings.executor_timeout_seconds,
        action="cancel",
        execution_id=execution_id,
    )


async def submit_execution_artifact(
    settings: AgentSettings,
    execution_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    logger.debug("[DEBUG] Execution Submit Artifact")
    return await _post_json(
        _format_execution_url(settings.executor_artifacts_url, execution_id),
        payload,
        timeout_seconds=settings.executor_timeout_seconds,
        action="artifact",
        execution_id=execution_id,
    )


__all__ = [
    "ExecutorSubmitError",
    "get_execution",
    "get_execution_result",
    "get_execution_notebook",
    "submit_execution_cancel",
    "submit_execution_artifact",
    "submit_execution_continue",
    "submit_execution_finish",
    "submit_execution_start",
]