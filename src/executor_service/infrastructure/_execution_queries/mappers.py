"""ORM-to-view mappings shared by execution query readers."""

from typing import Any

from executor_service.application.execution_queries import (
    ExecutionArtifactView,
    ExecutionAttemptView,
    ExecutionDetailView,
    ExecutionOperationView,
    ExecutionStepAttemptView,
    ExecutionSummaryView,
)
from executor_service.infrastructure.db.models import (
    ExecutionArtifactORM,
    ExecutionAttemptORM,
    ExecutionOperationORM,
    ExecutionORM,
    ExecutionStepAttemptORM,
)

EXECUTION_SUMMARY_COLUMNS = (
    ExecutionORM.id,
    ExecutionORM.operation_mode,
    ExecutionORM.operation_wait_timeout_seconds,
    ExecutionORM.trigger_type,
    ExecutionORM.user_id,
    ExecutionORM.project_id,
    ExecutionORM.session_id,
    ExecutionORM.task_id,
    ExecutionORM.workflow_id,
    ExecutionORM.status,
    ExecutionORM.version,
    ExecutionORM.created_by_type,
    ExecutionORM.created_by,
    ExecutionORM.updated_by_type,
    ExecutionORM.updated_by,
    ExecutionORM.created_at,
    ExecutionORM.updated_at,
    ExecutionORM.started_at,
    ExecutionORM.finished_at,
)

EXECUTION_DETAIL_COLUMNS = (
    *EXECUTION_SUMMARY_COLUMNS,
    ExecutionORM.runtime_type,
    ExecutionORM.runtime_pool,
    ExecutionORM.runtime_profile,
    ExecutionORM.runtime_target_id,
    ExecutionORM.runtime_session_id,
    ExecutionORM.cancellation_reason,
    ExecutionORM.workspace_path,
    ExecutionORM.notebook_path,
    ExecutionORM.notebook_projection_status,
    ExecutionORM.notebook_projection_attempt_count,
    ExecutionORM.notebook_projection_error,
    ExecutionORM.notebook_projected_at,
    ExecutionORM.failure_type,
    ExecutionORM.error_message,
    ExecutionORM.retry_strategy,
    ExecutionORM.retry_count,
    ExecutionORM.retry_from_sequence,
    ExecutionORM.retained_runtime_session_until,
    ExecutionORM.recovery_count,
    ExecutionORM.runtime_session_cleanup_status,
    ExecutionORM.runtime_abort_status,
    ExecutionORM.operation_wait_expires_at,
    ExecutionORM.execution_expires_at,
)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _is_secret_key(key) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def execution_summary_view(
    row: ExecutionORM, step_count: int
) -> ExecutionSummaryView:
    return ExecutionSummaryView(
        id=row.id,
        operation_mode=row.operation_mode,
        operation_wait_timeout_seconds=row.operation_wait_timeout_seconds,
        trigger_type=row.trigger_type,
        user_id=row.user_id,
        project_id=row.project_id,
        session_id=row.session_id,
        task_id=row.task_id,
        workflow_id=row.workflow_id,
        status=row.status,
        version=row.version,
        step_count=step_count,
        created_by_type=row.created_by_type,
        created_by=row.created_by,
        updated_by_type=row.updated_by_type,
        updated_by=row.updated_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def execution_detail_view(row: ExecutionORM) -> ExecutionDetailView:
    return ExecutionDetailView(
        id=row.id,
        operation_mode=row.operation_mode,
        operation_wait_timeout_seconds=row.operation_wait_timeout_seconds,
        trigger_type=row.trigger_type,
        user_id=row.user_id,
        project_id=row.project_id,
        session_id=row.session_id,
        task_id=row.task_id,
        workflow_id=row.workflow_id,
        runtime_type=row.runtime_type,
        runtime_pool=row.runtime_pool,
        runtime_profile=row.runtime_profile,
        runtime_target_id=row.runtime_target_id,
        runtime_session_id=row.runtime_session_id,
        status=row.status,
        version=row.version,
        cancellation_reason=row.cancellation_reason,
        workspace_path=row.workspace_path,
        notebook_path=row.notebook_path,
        notebook_projection_status=row.notebook_projection_status,
        notebook_projection_attempt_count=(
            row.notebook_projection_attempt_count
        ),
        notebook_projection_error=row.notebook_projection_error,
        notebook_projected_at=row.notebook_projected_at,
        failure_type=row.failure_type,
        error_message=row.error_message,
        retry_strategy=row.retry_strategy,
        retry_count=row.retry_count,
        retry_from_sequence=row.retry_from_sequence,
        retained_runtime_session_until=row.retained_runtime_session_until,
        recovery_count=row.recovery_count,
        runtime_session_cleanup_status=row.runtime_session_cleanup_status,
        runtime_abort_status=row.runtime_abort_status,
        operation_wait_expires_at=row.operation_wait_expires_at,
        execution_expires_at=row.execution_expires_at,
        created_by_type=row.created_by_type,
        created_by=row.created_by,
        updated_by_type=row.updated_by_type,
        updated_by=row.updated_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def artifact_view(row: ExecutionArtifactORM) -> ExecutionArtifactView:
    return ExecutionArtifactView(
        id=row.id,
        execution_id=row.execution_id,
        execution_attempt_id=row.execution_attempt_id,
        execution_step_id=row.execution_step_id,
        execution_step_attempt_id=row.execution_step_attempt_id,
        parent_artifact_id=row.parent_artifact_id,
        external_parent_asset_id=row.external_parent_asset_id,
        artifact_type=row.artifact_type,
        storage_type=row.storage_type,
        status=row.status,
        name=row.name,
        description=row.description,
        uri=row.uri,
        relative_path=row.relative_path,
        media_type=row.media_type,
        size_bytes=row.size_bytes,
        checksum_sha256=row.checksum_sha256,
        metadata=redact(row.artifact_metadata),
        created_by_type=row.created_by_type,
        created_by=row.created_by,
        updated_by_type=row.updated_by_type,
        updated_by=row.updated_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def attempt_view(
    row: ExecutionAttemptORM, step_count: int
) -> ExecutionAttemptView:
    return ExecutionAttemptView(
        id=row.id,
        execution_id=row.execution_id,
        attempt_number=row.attempt_number,
        runtime_type=row.runtime_type,
        runtime_profile=row.runtime_profile,
        runtime_target_id=row.runtime_target_id,
        runtime_session_id=row.runtime_session_id,
        status=row.status,
        lease_owner=row.lease_owner,
        lease_expires_at=row.lease_expires_at,
        heartbeat_at=row.heartbeat_at,
        error_message=row.error_message,
        failure_type=row.failure_type,
        retry_strategy=row.retry_strategy,
        runtime_session_cleanup_status=row.runtime_session_cleanup_status,
        runtime_abort_status=row.runtime_abort_status,
        created_by_type=row.created_by_type,
        created_by=row.created_by,
        updated_by_type=row.updated_by_type,
        updated_by=row.updated_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        step_count=step_count,
    )


def operation_view(row: ExecutionOperationORM) -> ExecutionOperationView:
    return ExecutionOperationView(
        id=row.id,
        execution_id=row.execution_id,
        operation_number=row.operation_number,
        schema_version=row.schema_version,
        first_sequence=row.first_sequence,
        last_sequence=row.last_sequence,
        operation_timeout_seconds=row.operation_timeout_seconds,
        metadata=row.operation_metadata,
        status=row.status,
        execution_attempt_id=row.execution_attempt_id,
        error_message=row.error_message,
        created_by_type=row.created_by_type,
        created_by=row.created_by,
        updated_by_type=row.updated_by_type,
        updated_by=row.updated_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        step_count=row.last_sequence - row.first_sequence + 1,
    )


def step_attempt_view(
    row: ExecutionStepAttemptORM,
) -> ExecutionStepAttemptView:
    return ExecutionStepAttemptView(
        id=row.id,
        execution_id=row.execution_id,
        execution_attempt_id=row.execution_attempt_id,
        execution_step_id=row.execution_step_id,
        sequence=row.sequence,
        skill_name=row.skill_name,
        tool_name=row.tool_name,
        input_parameters=redact(row.input_parameters),
        status=row.status,
        output_summary=redact(row.output_summary),
        result_manifest_path=row.result_manifest_path,
        result_manifest_checksum_sha256=(row.result_manifest_checksum_sha256),
        result_manifest_size_bytes=row.result_manifest_size_bytes,
        result_fencing_token=row.result_fencing_token,
        result_complete=row.result_complete,
        result_representation_count=row.result_representation_count,
        result_total_size_bytes=row.result_total_size_bytes,
        error_message=row.error_message,
        created_by_type=row.created_by_type,
        created_by=row.created_by,
        updated_by_type=row.updated_by_type,
        updated_by=row.updated_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def _is_secret_key(key: str) -> bool:
    normalized = key.lower()
    return any(
        marker in normalized
        for marker in ("token", "secret", "password", "credential")
    )
