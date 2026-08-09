"""Transport-neutral ExecutionSpec v1 contracts and shared-PV resolver."""

import asyncio
import hashlib
import hmac
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from executor_service.domain.enums import CodeSourceType
from executor_service.domain.errors import InvalidExecutionSpecError


class ExecutionSpecModel(BaseModel):
    """Strict JSON contract shared by MCP and REST transport adapters."""

    model_config = ConfigDict(extra="forbid")


class ExecutionStepInput(ExecutionSpecModel):
    sequence: int = Field(ge=0)
    plan_step_id: str = Field(min_length=1, max_length=255)
    skill_name: str | None = Field(default=None, max_length=255)
    tool_name: str | None = Field(default=None, max_length=255)
    input_parameters: dict[str, Any] = Field(default_factory=dict)
    code: str = Field(min_length=1)


class ExecutionSpec(ExecutionSpecModel):
    schema_version: Literal["1.0"]
    execution_plan_id: str = Field(min_length=1, max_length=255)
    steps: list[ExecutionStepInput] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_steps(self) -> Self:
        sequences = [step.sequence for step in self.steps]
        expected = list(range(sequences[0], sequences[0] + len(sequences)))
        if sequences != expected:
            raise ValueError("Step sequence values must be contiguous and ordered.")
        plan_step_ids = [step.plan_step_id for step in self.steps]
        if len(plan_step_ids) != len(set(plan_step_ids)):
            raise ValueError("plan_step_id values must be unique within an ExecutionSpec.")
        if any(not step.code.strip() for step in self.steps):
            raise ValueError("Step code must not be blank.")
        return self


class InlineCodeSource(ExecutionSpecModel):
    type: Literal[CodeSourceType.INLINE]
    spec: ExecutionSpec


class PathCodeSource(ExecutionSpecModel):
    type: Literal[CodeSourceType.PATH]
    path: str = Field(min_length=1, max_length=4096)
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")


CodeSource = Annotated[InlineCodeSource | PathCodeSource, Field(discriminator="type")]


@dataclass(frozen=True, slots=True)
class ResolvedExecutionSpec:
    spec: ExecutionSpec
    canonical_content: str
    sha256: str


class ExecutionSpecResolver:
    def __init__(
        self,
        workspace_root: Path,
        *,
        inline_max_bytes: int = 256 * 1024,
        file_max_bytes: int = 50 * 1024 * 1024,
    ) -> None:
        self._workspace_root = workspace_root.resolve()
        self._inline_max_bytes = inline_max_bytes
        self._file_max_bytes = file_max_bytes

    async def resolve(self, source: CodeSource) -> ResolvedExecutionSpec:
        if isinstance(source, InlineCodeSource):
            content = source.spec.model_dump_json()
            encoded = content.encode("utf-8")
            if len(encoded) > self._inline_max_bytes:
                raise InvalidExecutionSpecError(
                    "INLINE ExecutionSpec exceeds the configured size limit; use PATH."
                )
            return ResolvedExecutionSpec(
                spec=source.spec,
                canonical_content=content,
                sha256=hashlib.sha256(encoded).hexdigest(),
            )
        if not isinstance(source, PathCodeSource):
            raise InvalidExecutionSpecError("Unsupported ExecutionSpec source type.")
        return await asyncio.to_thread(self._resolve_path, source)

    def _resolve_path(self, source: PathCodeSource) -> ResolvedExecutionSpec:
        relative_path = Path(source.path)
        if relative_path.is_absolute():
            raise InvalidExecutionSpecError("PATH source must be relative to the shared PV root.")
        resolved_path = (self._workspace_root / relative_path).resolve()
        try:
            resolved_path.relative_to(self._workspace_root)
        except ValueError as exc:
            raise InvalidExecutionSpecError(
                "PATH source resolves outside the shared PV root."
            ) from exc
        if not resolved_path.is_file():
            raise InvalidExecutionSpecError("ExecutionSpec PATH source does not exist.")
        with resolved_path.open("rb") as file:
            content = file.read(self._file_max_bytes + 1)
        if len(content) > self._file_max_bytes:
            raise InvalidExecutionSpecError(
                "PATH ExecutionSpec exceeds the configured file size limit."
            )
        digest = hashlib.sha256(content).hexdigest()
        if not hmac.compare_digest(digest, source.sha256.lower()):
            raise InvalidExecutionSpecError("ExecutionSpec SHA-256 does not match the file.")
        try:
            spec = ExecutionSpec.model_validate_json(content)
        except (ValidationError, UnicodeDecodeError) as exc:
            raise InvalidExecutionSpecError(
                "PATH source is not a valid ExecutionSpec v1 JSON file."
            ) from exc
        return ResolvedExecutionSpec(
            spec=spec,
            canonical_content=spec.model_dump_json(),
            sha256=digest,
        )
