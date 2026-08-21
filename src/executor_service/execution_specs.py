"""Transport-neutral ExecutionSpec 1.0 with per-Step content sources."""

import asyncio
import hashlib
import hmac
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from executor_service.domain.enums import CodeSourceType, StepPayloadType
from executor_service.domain.errors import InvalidExecutionSpecError


class ExecutionSpecModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StepLineage(ExecutionSpecModel):
    skill_name: str | None = Field(default=None, max_length=255)
    tool_name: str | None = Field(default=None, max_length=255)
    input_parameters: dict[str, Any] = Field(default_factory=dict)


class InlineStepSource(ExecutionSpecModel):
    type: Literal[CodeSourceType.INLINE]
    content: str = Field(min_length=1)


class PathStepSource(ExecutionSpecModel):
    type: Literal[CodeSourceType.PATH]
    path: str = Field(min_length=1, max_length=4096)
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")


StepSource = Annotated[InlineStepSource | PathStepSource, Field(discriminator="type")]


class PythonExecutePayload(ExecutionSpecModel):
    type: Literal[StepPayloadType.PYTHON_EXECUTE]
    source: StepSource


StepPayload = Annotated[PythonExecutePayload, Field(discriminator="type")]


class ExecutionStepInput(ExecutionSpecModel):
    sequence: int = Field(ge=0)
    payload: StepPayload
    step_timeout_seconds: int | None = Field(default=None, ge=1)
    lineage: StepLineage | None = None


class ExecutionSpec(ExecutionSpecModel):
    schema_version: Literal["1.0"]
    steps: list[ExecutionStepInput] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_steps(self) -> Self:
        sequences = [step.sequence for step in self.steps]
        expected = list(range(sequences[0], sequences[0] + len(sequences)))
        if sequences != expected:
            raise ValueError("Step sequence values must be contiguous and ordered.")
        return self


@dataclass(frozen=True, slots=True)
class ResolvedExecutionStep:
    sequence: int
    content: str
    source_type: CodeSourceType
    source_path: str | None
    source_sha256: str
    step_timeout_seconds: int | None
    skill_name: str | None
    tool_name: str | None
    input_parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ResolvedExecutionSpec:
    spec: ExecutionSpec
    steps: tuple[ResolvedExecutionStep, ...]


class ExecutionSpecResolver:
    def __init__(
        self,
        input_root: Path,
        *,
        inline_max_bytes: int = 256 * 1024,
        file_max_bytes: int = 50 * 1024 * 1024,
    ) -> None:
        self._input_root = input_root.resolve()
        self._inline_max_bytes = inline_max_bytes
        self._file_max_bytes = file_max_bytes

    async def resolve(self, spec: ExecutionSpec) -> ResolvedExecutionSpec:
        resolved = tuple([await self._resolve_step(step) for step in spec.steps])
        return ResolvedExecutionSpec(spec=spec, steps=resolved)

    async def _resolve_step(self, step: ExecutionStepInput) -> ResolvedExecutionStep:
        source = step.payload.source
        if isinstance(source, InlineStepSource):
            content = source.content
            encoded = content.encode("utf-8")
            if len(encoded) > self._inline_max_bytes:
                raise InvalidExecutionSpecError(
                    "INLINE Step source exceeds the configured size limit; use PATH."
                )
            source_type = CodeSourceType.INLINE
            source_path = None
            checksum = hashlib.sha256(encoded).hexdigest()
        elif isinstance(source, PathStepSource):
            content, checksum = await asyncio.to_thread(self._read_path, source)
            source_type = CodeSourceType.PATH
            source_path = source.path
        else:  # pragma: no cover
            raise InvalidExecutionSpecError("Unsupported Step source type.")
        if not content.strip():
            raise InvalidExecutionSpecError("Python Step source must not be blank.")
        lineage = step.lineage
        return ResolvedExecutionStep(
            sequence=step.sequence,
            content=content,
            source_type=source_type,
            source_path=source_path,
            source_sha256=checksum,
            step_timeout_seconds=step.step_timeout_seconds,
            skill_name=lineage.skill_name if lineage else None,
            tool_name=lineage.tool_name if lineage else None,
            input_parameters=lineage.input_parameters if lineage else {},
        )

    def _read_path(self, source: PathStepSource) -> tuple[str, str]:
        candidate = Path(source.path)
        if candidate.is_absolute():
            raise InvalidExecutionSpecError("PATH Step source must be relative to the input root.")
        try:
            resolved = (self._input_root / candidate).resolve()
            resolved.relative_to(self._input_root)
        except ValueError as exc:
            raise InvalidExecutionSpecError(
                "PATH Step source resolves outside the input root."
            ) from exc
        if resolved.suffix.lower() != ".py":
            raise InvalidExecutionSpecError("PATH Python Step source must use a .py file.")
        if not resolved.is_file():
            raise InvalidExecutionSpecError("PATH Python Step source does not exist.")
        if resolved.stat().st_size > self._file_max_bytes:
            raise InvalidExecutionSpecError(
                "PATH Python Step source exceeds the configured file size limit."
            )
        try:
            encoded = resolved.read_bytes()
            content = encoded.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InvalidExecutionSpecError("PATH Python Step source must be UTF-8.") from exc
        checksum = hashlib.sha256(encoded).hexdigest()
        if not hmac.compare_digest(checksum, source.sha256.lower()):
            raise InvalidExecutionSpecError("Python Step source SHA-256 does not match the file.")
        return content, checksum
