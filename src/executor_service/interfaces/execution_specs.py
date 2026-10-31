"""Transport-neutral ExecutionSpec 1.0 with per-Step content sources."""

import asyncio
import hashlib
import hmac
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from executor_service.domain.enums import CodeSourceType, StepPayloadType
from executor_service.domain.errors import (
    ContextValue,
    InvalidExecutionSpecError,
)
from executor_service.interfaces.bounded_mappings import BoundedParameters


class ExecutionSpecModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StepLineage(ExecutionSpecModel):
    skill_name: str | None = Field(default=None, max_length=255)
    tool_name: str | None = Field(default=None, max_length=255)
    input_parameters: BoundedParameters = Field(default_factory=dict)


class InlineStepSource(ExecutionSpecModel):
    type: Literal[CodeSourceType.INLINE]
    content: str = Field(min_length=1)


class PathStepSource(ExecutionSpecModel):
    type: Literal[CodeSourceType.PATH]
    path: str = Field(
        min_length=1,
        max_length=4096,
        description=(
            "Python file path relative to INPUT_STORAGE_ROOT "
            "(defaults to SHARED_STORAGE_ROOT); no prefix is added."
        ),
    )
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")


StepSource = Annotated[
    InlineStepSource | PathStepSource, Field(discriminator="type")
]


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
            raise ValueError(
                "Step sequence values must be contiguous and ordered."
            )
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
        max_steps: int = 100,
    ) -> None:
        self._input_root = input_root.resolve()
        self._inline_max_bytes = inline_max_bytes
        self._file_max_bytes = file_max_bytes
        self._max_steps = max_steps

    async def resolve(
        self,
        spec: ExecutionSpec,
        *,
        location: tuple[str | int, ...] = ("body", "spec"),
    ) -> ResolvedExecutionSpec:
        """``location`` is where ``spec`` sits in the request, for errors."""
        if len(spec.steps) > self._max_steps:
            raise InvalidExecutionSpecError.at(
                (*location, "steps"),
                f"An Operation may carry at most {self._max_steps} Steps; "
                f"this one has {len(spec.steps)}.",
                "too_many_steps",
                limit=self._max_steps,
                actual=len(spec.steps),
            )
        resolved = tuple(
            [
                await self._resolve_step(step, (*location, "steps", index))
                for index, step in enumerate(spec.steps)
            ]
        )
        return ResolvedExecutionSpec(spec=spec, steps=resolved)

    async def _resolve_step(
        self, step: ExecutionStepInput, location: tuple[str | int, ...]
    ) -> ResolvedExecutionStep:
        source = step.payload.source
        source_location = (*location, "payload", "source")
        if isinstance(source, InlineStepSource):
            content = source.content
            encoded = content.encode("utf-8")
            if len(encoded) > self._inline_max_bytes:
                raise InvalidExecutionSpecError.at(
                    (*source_location, "content"),
                    f"INLINE Step source is {len(encoded)} bytes, over the "
                    f"{self._inline_max_bytes}-byte limit; send it as PATH.",
                    "inline_source_too_large",
                    limit_bytes=self._inline_max_bytes,
                    actual_bytes=len(encoded),
                    sequence=step.sequence,
                )
            source_type = CodeSourceType.INLINE
            source_path = None
            checksum = hashlib.sha256(encoded).hexdigest()
        elif isinstance(source, PathStepSource):
            content, checksum = await asyncio.to_thread(
                self._read_path, source, source_location, step.sequence
            )
            source_type = CodeSourceType.PATH
            source_path = source.path
        else:  # pragma: no cover
            raise InvalidExecutionSpecError.at(
                source_location,
                "Unsupported Step source type.",
                "unsupported_source_type",
            )
        if not content.strip():
            raise InvalidExecutionSpecError.at(
                source_location,
                f"Step {step.sequence} source must not be blank.",
                "source_blank",
                sequence=step.sequence,
            )
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

    def _read_path(
        self,
        source: PathStepSource,
        location: tuple[str | int, ...],
        sequence: int,
    ) -> tuple[str, str]:
        path_location = (*location, "path")

        def invalid(
            message: str, type: str, **context: ContextValue
        ) -> InvalidExecutionSpecError:
            return InvalidExecutionSpecError.at(
                path_location,
                message,
                type,
                path=source.path,
                sequence=sequence,
                **context,
            )

        candidate = Path(source.path)
        if candidate.is_absolute():
            raise invalid(
                "PATH Step source must be relative to the shared PV root.",
                "path_not_relative",
            )
        try:
            resolved = (self._input_root / candidate).resolve()
            resolved.relative_to(self._input_root)
        except ValueError as exc:
            raise invalid(
                "PATH Step source resolves outside the shared PV root.",
                "path_outside_root",
            ) from exc
        if resolved.suffix.lower() != ".py":
            raise invalid(
                "PATH Python Step source must use a .py file.",
                "path_not_python",
            )
        if not resolved.is_file():
            raise invalid(
                "PATH Python Step source does not exist.", "path_not_found"
            )
        size = resolved.stat().st_size
        if size > self._file_max_bytes:
            raise invalid(
                f"PATH Python Step source is {size} bytes, over the "
                f"{self._file_max_bytes}-byte limit.",
                "path_source_too_large",
                limit_bytes=self._file_max_bytes,
                actual_bytes=size,
            )
        try:
            with resolved.open("rb") as handle:
                encoded = handle.read(self._file_max_bytes + 1)
            if len(encoded) > self._file_max_bytes:
                raise invalid(
                    "PATH Python Step source grew over the "
                    f"{self._file_max_bytes}-byte limit while being read.",
                    "path_source_too_large",
                    limit_bytes=self._file_max_bytes,
                )
            content = encoded.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise invalid(
                "PATH Python Step source must be UTF-8.", "path_not_utf8"
            ) from exc
        checksum = hashlib.sha256(encoded).hexdigest()
        if not hmac.compare_digest(checksum, source.sha256.lower()):
            raise InvalidExecutionSpecError.at(
                (*location, "sha256"),
                "Python Step source SHA-256 does not match the file.",
                "sha256_mismatch",
                path=source.path,
                sequence=sequence,
                expected=source.sha256.lower(),
                actual=checksum,
            )
        return content, checksum


# Where the submit body carries its ExecutionSpec; an Operation create body
# carries it at the top level (the resolver's default).
SUBMIT_SPEC_LOCATION: tuple[str, ...] = ("body", "operation", "spec")


def require_submit_starts_at_zero(resolved: ResolvedExecutionSpec) -> None:
    first = resolved.spec.steps[0].sequence
    if first != 0:
        raise InvalidExecutionSpecError.at(
            (*SUBMIT_SPEC_LOCATION, "steps", 0, "sequence"),
            "Execution submit requires an ExecutionSpec starting at "
            f"sequence 0; the first Step has sequence {first}.",
            "first_sequence_not_zero",
            expected=0,
            actual=first,
        )
