"""Execution submission transport contracts."""

import re
from typing import Any

from pydantic import Field, field_validator, model_validator

from executor_service.application.commands import (
    StepSpec,
    SubmitExecutionCommand,
)
from executor_service.domain.enums import (
    OperationMode,
    RuntimeType,
    TriggerType,
)
from executor_service.execution_specs import (
    ExecutionSpec,
    ResolvedExecutionSpec,
)
from executor_service.interfaces._contracts.common import (
    ActorInput,
    ContractModel,
)


class ExecutionContext(ContractModel):
    user_id: str = Field(min_length=1, max_length=255)
    task_id: str = Field(min_length=1, max_length=255)
    project_id: str | None = Field(default=None, min_length=1, max_length=255)
    session_id: str | None = Field(default=None, min_length=1, max_length=255)
    workflow_id: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def validate_scope(self) -> "ExecutionContext":
        if self.session_id is not None and self.project_id is None:
            raise ValueError("context.session_id requires context.project_id.")
        if self.project_id == "unscoped" or self.session_id == "unscoped":
            raise ValueError(
                "'unscoped' is reserved for Executor workspace paths."
            )
        return self


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


_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
# Names that would let a caller bypass the kernel launcher or its limits.
_RESERVED_ENV_PREFIXES = (
    "EXECUTOR_",
    "JPY_",
    "JUPYTER",
    "LD_",
    "MALLOC_",
    "PYTHON",
)
_RESERVED_ENV_NAMES = {"HOME", "PATH"}


class ExecutionRuntimeInput(ContractModel):
    type: RuntimeType
    profile: str = Field(min_length=1, max_length=128)
    memory_limit_bytes: int | None = Field(
        default=None,
        ge=256 * 1024**2,
        description=(
            "Kernel virtual memory cap. A kernel reserves about 0.5 GiB of "
            "address space beyond what it uses. Defaults to the Executor's "
            "EXECUTION_DEFAULT_MEMORY_LIMIT_BYTES."
        ),
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        max_length=64,
        description=(
            "Environment variables for this Execution's kernel only, such "
            "as storage credentials. Stored encrypted and never returned."
        ),
    )

    @field_validator("env")
    @classmethod
    def validate_env(cls, env: dict[str, str]) -> dict[str, str]:
        for name, value in env.items():
            if (
                not _ENV_NAME.fullmatch(name)
                or name in _RESERVED_ENV_NAMES
                or name.startswith(_RESERVED_ENV_PREFIXES)
            ):
                raise ValueError(f"runtime.env name '{name}' is not allowed.")
            if len(value) > 4096 or "\x00" in value:
                raise ValueError(f"runtime.env value for '{name}' is invalid.")
        return env


class ExecutionOperationInput(ContractModel):
    operation_timeout_seconds: int | None = Field(default=None, ge=1)
    spec: ExecutionSpec
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExecutionSubmitRequest(ContractModel):
    idempotency_key: str = Field(min_length=1, max_length=255)
    lifecycle: ExecutionLifecycleInput
    trigger: ExecutionTriggerInput
    runtime: ExecutionRuntimeInput
    context: ExecutionContext
    operation: ExecutionOperationInput
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_command(
        self, resolved: ResolvedExecutionSpec
    ) -> SubmitExecutionCommand:
        return SubmitExecutionCommand(
            idempotency_key=self.idempotency_key,
            operation_mode=self.lifecycle.operation_mode,
            operation_wait_timeout_seconds=(
                self.lifecycle.operation_wait_timeout_seconds
            ),
            trigger_type=self.trigger.type,
            runtime_type=self.runtime.type,
            runtime_profile=self.runtime.profile,
            memory_limit_bytes=self.runtime.memory_limit_bytes,
            runtime_env=self.runtime.env,
            user_id=self.context.user_id,
            project_id=self.context.project_id,
            session_id=self.context.session_id,
            task_id=self.context.task_id,
            spec_schema_version=resolved.spec.schema_version,
            operation_timeout_seconds=(
                self.operation.operation_timeout_seconds
            ),
            actor_type=self.trigger.actor.type,
            actor_id=self.trigger.actor.id,
            workflow_id=self.context.workflow_id,
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
