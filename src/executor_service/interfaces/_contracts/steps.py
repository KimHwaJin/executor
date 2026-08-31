"""Execution Step and StepAttempt transport contracts."""

from typing import Any, Literal
from uuid import UUID

from executor_service.application.execution_queries import (
    ExecutionStepAttemptView,
)
from executor_service.application.pagination import Page
from executor_service.domain.enums import CodeSourceType, StepStatus
from executor_service.domain.models import ExecutionStep
from executor_service.interfaces._contracts.common import (
    AuditFields,
    ContractModel,
    Lifecycle,
    PageResponse,
)
from executor_service.result_summaries import OutputSummary


class ToolReference(ContractModel):
    skill_name: str | None
    tool_name: str | None
    input_parameters: dict[str, Any]


class StepResultReference(ContractModel):
    """Immutable Step result manifest in the Agent/Executor shared volume."""

    storage: Literal["SHARED_PV"] = "SHARED_PV"
    execution_id: UUID
    step_id: UUID
    attempt_id: UUID
    fencing_token: int
    relative_path: str
    checksum_sha256: str
    size_bytes: int
    complete: bool
    representation_count: int
    total_size_bytes: int


class StepResultSummary(ContractModel):
    status: StepStatus
    output_summary: OutputSummary
    error_message: str | None


class StepResult(StepResultSummary):
    result_ref: StepResultReference | None


class StepSourceResponse(ContractModel):
    type: CodeSourceType
    path: str | None
    sha256: str


class ExecutionStepResponse(AuditFields):
    step_id: UUID
    execution_id: UUID
    sequence: int
    code_hash: str | None
    source: StepSourceResponse
    step_timeout_seconds: int | None
    lineage: ToolReference
    result: StepResult
    lifecycle: Lifecycle

    @classmethod
    def from_domain(
        cls,
        step: ExecutionStep,
        execution_id: UUID,
    ) -> "ExecutionStepResponse":
        return cls(
            step_id=step.id,
            execution_id=execution_id,
            sequence=step.sequence,
            code_hash=step.code_hash,
            source=StepSourceResponse(
                type=step.source_type,
                path=step.source_path,
                sha256=step.source_sha256,
            ),
            step_timeout_seconds=step.step_timeout_seconds,
            lineage=ToolReference(
                skill_name=step.skill_name,
                tool_name=step.tool_name,
                input_parameters=step.input_parameters,
            ),
            result=StepResult(
                status=step.status,
                output_summary=OutputSummary.model_validate(
                    step.output_summary
                ),
                result_ref=(
                    StepResultReference(
                        execution_id=execution_id,
                        step_id=step.id,
                        attempt_id=step.result_execution_attempt_id,
                        fencing_token=step.result_fencing_token,
                        relative_path=step.result_manifest_path,
                        checksum_sha256=(step.result_manifest_checksum_sha256),
                        size_bytes=step.result_manifest_size_bytes,
                        complete=step.result_complete,
                        representation_count=(
                            step.result_representation_count
                        ),
                        total_size_bytes=step.result_total_size_bytes,
                    )
                    if (
                        step.result_execution_attempt_id is not None
                        and step.result_fencing_token is not None
                        and step.result_manifest_path is not None
                        and step.result_manifest_checksum_sha256 is not None
                        and step.result_manifest_size_bytes is not None
                        and step.result_complete is not None
                    )
                    else None
                ),
                error_message=step.error_message,
            ),
            lifecycle=Lifecycle(
                started_at=step.started_at,
                finished_at=step.finished_at,
            ),
            created_by_type=step.created_by_type,
            created_by=step.created_by,
            updated_by_type=step.updated_by_type,
            updated_by=step.updated_by,
            created_at=step.created_at,
            updated_at=step.updated_at,
        )


class ExecutionStepSummaryResponse(AuditFields):
    step_id: UUID
    execution_id: UUID
    sequence: int
    lineage: ToolReference
    result: StepResultSummary
    lifecycle: Lifecycle

    @classmethod
    def from_domain(
        cls,
        step: ExecutionStep,
        execution_id: UUID,
    ) -> "ExecutionStepSummaryResponse":
        return cls(
            step_id=step.id,
            execution_id=execution_id,
            sequence=step.sequence,
            lineage=ToolReference(
                skill_name=step.skill_name,
                tool_name=step.tool_name,
                input_parameters=step.input_parameters,
            ),
            result=StepResultSummary(
                status=step.status,
                output_summary=OutputSummary.model_validate(
                    step.output_summary
                ),
                error_message=step.error_message,
            ),
            lifecycle=Lifecycle(
                started_at=step.started_at,
                finished_at=step.finished_at,
            ),
            created_by_type=step.created_by_type,
            created_by=step.created_by,
            updated_by_type=step.updated_by_type,
            updated_by=step.updated_by,
            created_at=step.created_at,
            updated_at=step.updated_at,
        )


def _step_attempt_common(view: ExecutionStepAttemptView) -> dict[str, Any]:
    return {
        "step_attempt_id": view.id,
        "execution_step_id": view.execution_step_id,
        "sequence": view.sequence,
        "tool": ToolReference(
            skill_name=view.skill_name,
            tool_name=view.tool_name,
            input_parameters=view.input_parameters,
        ),
        "result": StepResult(
            status=view.status,
            output_summary=OutputSummary.model_validate(view.output_summary),
            result_ref=(
                StepResultReference(
                    execution_id=view.execution_id,
                    step_id=view.execution_step_id,
                    attempt_id=view.execution_attempt_id,
                    fencing_token=view.result_fencing_token,
                    relative_path=view.result_manifest_path,
                    checksum_sha256=view.result_manifest_checksum_sha256,
                    size_bytes=view.result_manifest_size_bytes,
                    complete=view.result_complete,
                    representation_count=view.result_representation_count,
                    total_size_bytes=view.result_total_size_bytes,
                )
                if (
                    view.status
                    in {
                        StepStatus.SUCCEEDED,
                        StepStatus.FAILED,
                        StepStatus.CANCELLED,
                    }
                    and view.result_fencing_token is not None
                    and view.result_manifest_path is not None
                    and view.result_manifest_checksum_sha256 is not None
                    and view.result_manifest_size_bytes is not None
                    and view.result_complete is not None
                )
                else None
            ),
            error_message=view.error_message,
        ),
        "lifecycle": Lifecycle(
            started_at=view.started_at,
            finished_at=view.finished_at,
        ),
        "created_by_type": view.created_by_type,
        "created_by": view.created_by,
        "updated_by_type": view.updated_by_type,
        "updated_by": view.updated_by,
        "created_at": view.created_at,
        "updated_at": view.updated_at,
    }


class ExecutionStepAttemptResponse(AuditFields):
    step_attempt_id: UUID
    execution_step_id: UUID
    sequence: int
    tool: ToolReference
    result: StepResult
    lifecycle: Lifecycle

    @classmethod
    def from_view(
        cls,
        view: ExecutionStepAttemptView,
    ) -> "ExecutionStepAttemptResponse":
        return cls(**_step_attempt_common(view))


class ExecutionStepAttemptSummaryResponse(AuditFields):
    step_attempt_id: UUID
    execution_step_id: UUID
    sequence: int
    tool: ToolReference
    result: StepResult
    lifecycle: Lifecycle

    @classmethod
    def from_view(
        cls,
        view: ExecutionStepAttemptView,
    ) -> "ExecutionStepAttemptSummaryResponse":
        return cls(**_step_attempt_common(view))


class ExecutionStepPageResponse(PageResponse):
    items: list[ExecutionStepSummaryResponse]

    @classmethod
    def from_page(
        cls,
        page: Page[ExecutionStep],
        execution_id: UUID,
    ) -> "ExecutionStepPageResponse":
        return cls(
            items=[
                ExecutionStepSummaryResponse.from_domain(item, execution_id)
                for item in page.items
            ],
            next_cursor=page.next_cursor,
            has_more=page.next_cursor is not None,
        )


class ExecutionStepAttemptPageResponse(PageResponse):
    items: list[ExecutionStepAttemptSummaryResponse]

    @classmethod
    def from_page(
        cls,
        page: Page[ExecutionStepAttemptView],
    ) -> "ExecutionStepAttemptPageResponse":
        return cls(
            items=[
                ExecutionStepAttemptSummaryResponse.from_view(item)
                for item in page.items
            ],
            next_cursor=page.next_cursor,
            has_more=page.next_cursor is not None,
        )
