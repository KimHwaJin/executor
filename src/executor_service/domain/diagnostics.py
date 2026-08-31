"""Bounded Runtime-neutral failure observations, independent of state."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class DiagnosticCategory(StrEnum):
    EXECUTION = "EXECUTION"
    OUTPUT = "OUTPUT"
    NOTEBOOK = "NOTEBOOK"
    ARTIFACT = "ARTIFACT"
    CLEANUP = "CLEANUP"


class DiagnosticOrigin(StrEnum):
    RUNTIME = "RUNTIME"
    RESULT_STORAGE = "RESULT_STORAGE"
    EXECUTOR = "EXECUTOR"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class DiagnosticCause:
    exception_type: str
    message: str
    errno: int | None = None


@dataclass(frozen=True, slots=True)
class RuntimeDiagnostic:
    code: str
    phase: str
    category: DiagnosticCategory
    origin: DiagnosticOrigin
    message: str
    causes: tuple[DiagnosticCause, ...]
    causes_truncated: bool = False
    severity: Literal["ERROR"] = "ERROR"
