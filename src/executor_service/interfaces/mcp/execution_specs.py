"""Resolve inline or shared-PV execution specifications into one validated contract."""

import asyncio
import hashlib
import hmac
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from executor_service.domain.errors import InvalidExecutionSpecError
from executor_service.interfaces.mcp.schemas import (
    CodeSource,
    ExecutionSpec,
    InlineCodeSource,
    PathCodeSource,
)


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
