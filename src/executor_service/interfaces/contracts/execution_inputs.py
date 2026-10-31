"""Execution submission transport contracts."""

from pydantic import Field, model_validator

from executor_service.application.commands import (
    StepSpec,
    SubmitExecutionCommand,
)
from executor_service.domain.enums import (
    OperationMode,
    RuntimeType,
    TriggerType,
)
from executor_service.interfaces.auth import (
    Caller,
    require_tenant,
    require_user,
)
from executor_service.interfaces.bounded_mappings import CallerMetadata
from executor_service.interfaces.contracts.common import (
    ActorInput,
    ContractModel,
)
from executor_service.interfaces.execution_specs import (
    ExecutionSpec,
    ResolvedExecutionSpec,
)


class ExecutionLifecycleInput(ContractModel):
    operation_mode: OperationMode
    operation_wait_timeout_seconds: int | None = Field(default=None, ge=30)

    @model_validator(mode="after")
    def validate_wait_timeout(self) -> "ExecutionLifecycleInput":
        if self.operation_mode == OperationMode.MULTI:
            if self.operation_wait_timeout_seconds is None:
                raise ValueError(
                    "MULTI lifecycle requires operation_wait_timeout_seconds."
                )
        elif self.operation_wait_timeout_seconds is not None:
            raise ValueError(
                "SINGLE lifecycle does not accept operation_wait_timeout_seconds."
            )
        return self


class ExecutionTriggerInput(ContractModel):
    type: TriggerType
    actor: ActorInput


class ExecutionRuntimeInput(ContractModel):
    type: RuntimeType
    profile: str = Field(min_length=1, max_length=128)


class ExecutionOperationInput(ContractModel):
    operation_timeout_seconds: int | None = Field(default=None, ge=1)
    spec: ExecutionSpec
    metadata: CallerMetadata = Field(default_factory=dict)


class ExecutionSubmitRequest(ContractModel):
    idempotency_key: str = Field(min_length=1, max_length=255)
    lifecycle: ExecutionLifecycleInput
    trigger: ExecutionTriggerInput
    runtime: ExecutionRuntimeInput
    operation: ExecutionOperationInput
    metadata: CallerMetadata = Field(
        default_factory=dict,
        description=(
            "Caller-owned identifiers such as batch or run IDs, stored as "
            "given and echoed in execution.completed. At most 32 keys "
            "matching ^[a-z][a-z0-9_]{0,63}$, 4096 serialized bytes."
        ),
    )

    def to_command(
        self, resolved: ResolvedExecutionSpec, caller: Caller
    ) -> SubmitExecutionCommand:
        """The caller's headers, never the body, name the tenant and context."""

        return SubmitExecutionCommand(
            idempotency_key=self.idempotency_key,
            operation_mode=self.lifecycle.operation_mode,
            operation_wait_timeout_seconds=(
                self.lifecycle.operation_wait_timeout_seconds
            ),
            trigger_type=self.trigger.type,
            runtime_type=self.runtime.type,
            runtime_profile=self.runtime.profile,
            user_id=require_user(caller),
            project_id=caller.project_id,
            session_id=caller.session_id,
            spec_schema_version=resolved.spec.schema_version,
            operation_timeout_seconds=(
                self.operation.operation_timeout_seconds
            ),
            actor_type=self.trigger.actor.type,
            actor_id=self.trigger.actor.id,
            tenant_id=require_tenant(caller),
            metadata=self.metadata,
            operation_metadata=self.operation.metadata,
            steps=tuple(
                StepSpec(
                    sequence=step.sequence,
                    code=step.content,
                    source_type=step.source_type,
                    source_path=step.source_path,
                    source_sha256=step.source_sha256,
                    step_timeout_seconds=step.step_timeout_seconds,
                    skill_name=step.skill_name,
                    tool_name=step.tool_name,
                    input_parameters=step.input_parameters,
                )
                for step in resolved.steps
            ),
        )
