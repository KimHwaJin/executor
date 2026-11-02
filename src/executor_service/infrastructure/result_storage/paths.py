"""Safe path construction for shared execution result storage."""

import re
from pathlib import Path
from uuid import UUID

from executor_service.domain.results import StepResultIdentity
from executor_service.infrastructure.result_storage.errors import (
    ResultStorageError,
)

SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
# Executor's folder below the shared storage root; references include it.
RESULT_DIRECTORY = Path("executor", "executions")


class ResultStoragePaths:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def source_relative(self, execution_id: UUID, step_id: UUID) -> Path:
        return RESULT_DIRECTORY.joinpath(
            str(execution_id),
            "sources",
            str(step_id),
            "source.py",
        )

    def result_relative(self, identity: StepResultIdentity) -> Path:
        return RESULT_DIRECTORY.joinpath(
            str(identity.execution_id),
            "operations",
            str(identity.operation_id),
            "steps",
            str(identity.step_id),
            "attempts",
            str(identity.execution_attempt_id),
            str(identity.fencing_token),
        )

    def result_directory(
        self, identity: StepResultIdentity, *, must_exist: bool = False
    ) -> Path:
        return self.resolve_relative(
            self.result_relative(identity), must_exist=must_exist
        )

    def partial_directory(
        self, identity: StepResultIdentity, *, must_exist: bool = False
    ) -> Path:
        relative = self.result_relative(identity)
        partial = relative.with_name(f"{relative.name}.partial")
        return self.resolve_relative(partial, must_exist=must_exist)

    def resolve_reference(self, raw: str) -> Path:
        candidate = Path(raw)
        if candidate.is_absolute():
            raise ResultStorageError(
                "Shared result reference must be relative."
            )
        return self.resolve_relative(candidate, must_exist=True)

    def resolve_relative(
        self, relative: Path, *, must_exist: bool = False
    ) -> Path:
        if relative.is_absolute() or not relative.parts:
            raise ResultStorageError("Shared result path must be relative.")
        if not relative.is_relative_to(RESULT_DIRECTORY):
            raise ResultStorageError(
                "Shared result path must stay below Executor's result folder."
            )
        if any(
            part in {"", ".", ".."} or not SAFE_SEGMENT.fullmatch(part)
            for part in relative.parts
        ):
            raise ResultStorageError(
                "Shared result path contains an unsafe segment."
            )
        resolved = (self.root / relative).resolve(strict=must_exist)
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ResultStorageError(
                "Shared result path escapes its root."
            ) from exc
        return resolved
