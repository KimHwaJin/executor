"""Shared payload builders for public Execution events."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from executor_service.domain.results import StepResultDescriptor
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionOperationORM,
    ExecutionStepAttemptORM,
)


async def attempt_payload(
    session: AsyncSession, attempt_id: UUID
) -> dict[str, object]:
    attempt = await session.get(ExecutionAttemptORM, attempt_id)
    if attempt is None:
        raise ValueError(f"Execution Attempt {attempt_id} was not found.")
    return {
        "id": str(attempt.id),
        "number": attempt.attempt_number,
        "reason": "INITIAL" if attempt.attempt_number == 1 else "RETRY",
    }


async def operation_payload(
    session: AsyncSession, operation_id: UUID
) -> dict[str, object]:
    operation = await session.get(ExecutionOperationORM, operation_id)
    if operation is None:
        raise ValueError(f"Execution Operation {operation_id} was not found.")
    return {"id": str(operation.id), "number": operation.operation_number}


def stored_result_reference(
    stored_result: StepResultDescriptor,
) -> dict[str, object]:
    return {
        "storage": "SHARED_PV",
        "relative_path": stored_result.reference.relative_path,
        "media_type": "application/json",
        "size_bytes": stored_result.reference.size_bytes,
        "checksum_sha256": stored_result.reference.checksum_sha256,
        "complete": stored_result.complete,
    }


def row_result_reference(
    row: ExecutionStepAttemptORM,
) -> dict[str, object] | None:
    if not (
        row.result_complete is not None
        and row.result_manifest_path is not None
        and row.result_manifest_checksum_sha256 is not None
        and row.result_manifest_size_bytes is not None
    ):
        return None
    return {
        "storage": "SHARED_PV",
        "relative_path": row.result_manifest_path,
        "media_type": "application/json",
        "size_bytes": row.result_manifest_size_bytes,
        "checksum_sha256": row.result_manifest_checksum_sha256,
        "complete": row.result_complete,
    }


def event_output_summary(
    stored_result: StepResultDescriptor,
) -> dict[str, object]:
    mime_types = stored_result.output_summary.get("mime_types", [])
    return {
        "count": stored_result.output_count,
        "content_types": (
            sorted(set(mime_types)) if isinstance(mime_types, list) else []
        ),
    }
