"""Validate internal Redis work envelopes before dispatch."""

from uuid import UUID

from executor_service.work_messages import (
    WORK_MESSAGE_SCHEMA_VERSION,
    WorkStreamEnvelope,
)

DISPATCH_MESSAGE_TYPES = frozenset(
    {
        "operation.ready",
        "execution.finalization_ready",
        "execution.retry_ready",
        "execution.cancellation_ready",
    }
)
RUN_MESSAGE_TYPES = DISPATCH_MESSAGE_TYPES - {"execution.cancellation_ready"}


def invalid_work_message_reason(fields: dict[str, str]) -> str | None:
    message_id = fields.get("message_id")
    if not message_id:
        return "missing_message_id"
    try:
        UUID(message_id)
    except ValueError:
        return "invalid_message_id"
    if fields.get("aggregate_type") != "Execution":
        return "unsupported_aggregate_type"
    aggregate_id = fields.get("aggregate_id")
    if not aggregate_id:
        return "missing_aggregate_id"
    try:
        UUID(aggregate_id)
    except ValueError:
        return "invalid_aggregate_id"
    message_type = fields.get("message_type")
    if not message_type:
        return "missing_message_type"
    if message_type not in DISPATCH_MESSAGE_TYPES:
        return "unsupported_message_type"
    schema_version = fields.get("schema_version")
    if not schema_version:
        return "missing_schema_version"
    if schema_version != WORK_MESSAGE_SCHEMA_VERSION:
        return "unsupported_schema_version"
    if not fields.get("payload"):
        return "missing_payload"
    try:
        WorkStreamEnvelope.from_redis_fields(fields)
    except (TypeError, ValueError):
        return "invalid_work_message_contract"
    return None


def valid_uuid_or_empty(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(UUID(value))
    except ValueError:
        return ""
