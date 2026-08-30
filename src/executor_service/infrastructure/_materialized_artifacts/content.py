"""Resolve and validate Agent-authored Artifact text input."""

import asyncio
import hashlib
from pathlib import Path

from executor_service.application.commands import MaterializeArtifactCommand
from executor_service.domain.enums import CodeSourceType
from executor_service.domain.errors import ArtifactRegistrationError


class ArtifactContentResolver:
    def __init__(self, input_root: Path, *, max_bytes: int) -> None:
        self._input_root = input_root.resolve()
        self._max_bytes = max_bytes

    async def resolve(self, command: MaterializeArtifactCommand) -> str:
        if command.source_type == CodeSourceType.INLINE:
            if (
                command.source_content is None
                or command.source_path is not None
            ):
                raise ArtifactRegistrationError(
                    "INLINE Artifact source requires content only."
                )
            content = command.source_content
        else:
            if (
                command.source_path is None
                or command.source_content is not None
            ):
                raise ArtifactRegistrationError(
                    "PATH Artifact source requires path only."
                )
            path = Path(command.source_path)
            if path.is_absolute():
                raise ArtifactRegistrationError(
                    "Artifact input path must be relative."
                )
            resolved = (self._input_root / path).resolve()
            try:
                resolved.relative_to(self._input_root)
            except ValueError as exc:
                raise ArtifactRegistrationError(
                    "Artifact input path escapes the input root."
                ) from exc
            if not resolved.is_file():
                raise ArtifactRegistrationError(
                    "Artifact input file was not found."
                )
            raw = await asyncio.to_thread(resolved.read_bytes)
            if command.source_sha256 is not None and not same_hash(
                raw, command.source_sha256
            ):
                raise ArtifactRegistrationError(
                    "Artifact input SHA-256 does not match."
                )
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ArtifactRegistrationError(
                    "Artifact input must be UTF-8 text."
                ) from exc
        if not content.strip():
            raise ArtifactRegistrationError(
                "Artifact content must not be blank."
            )
        if len(content.encode()) > self._max_bytes:
            raise ArtifactRegistrationError(
                "Artifact content exceeds the configured size limit."
            )
        return content


def same_hash(raw: bytes, expected: str) -> bool:
    return hashlib.sha256(raw).hexdigest() == expected.lower()
