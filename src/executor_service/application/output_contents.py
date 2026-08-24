"""Validated access to Runtime-owned output representation content."""

import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

from executor_service.application.execution_queries import (
    ExecutionQueryService,
)
from executor_service.domain.enums import RuntimeType
from executor_service.domain.errors import (
    ExecutionOutputContentUnavailableError,
    ExecutionOutputNotFoundError,
)
from executor_service.domain.runtime import (
    RuntimeOutputContentAccess,
    RuntimeOutputContentChunk,
    RuntimeOutputJournalIdentity,
)


@dataclass(frozen=True, slots=True)
class ExecutionOutputContentDescriptor:
    execution_id: UUID
    output_id: UUID
    representation_id: UUID
    runtime_type: RuntimeType
    preferred_target_id: UUID
    identity: RuntimeOutputJournalIdentity
    journal_id: UUID
    media_type: str
    size_bytes: int
    checksum_sha256: str
    complete: bool


class ExecutionOutputContentService:
    def __init__(
        self,
        queries: ExecutionQueryService,
        runtime_storage: RuntimeOutputContentAccess,
    ) -> None:
        self._queries = queries
        self._runtime_storage = runtime_storage

    async def describe(
        self,
        execution_id: UUID,
        output_id: UUID,
        representation_id: UUID,
    ) -> ExecutionOutputContentDescriptor:
        execution = await self._queries.execution(execution_id)
        output = await self._queries.output(execution_id, output_id)
        representation = next(
            (
                item
                for item in output.representations
                if item.id == representation_id
            ),
            None,
        )
        if representation is None:
            raise ExecutionOutputNotFoundError(
                f"Representation {representation_id} was not found in "
                f"Execution Output {output_id}."
            )
        if execution.workspace_path is None:
            raise ExecutionOutputContentUnavailableError(
                f"Execution Output {output_id} has no Runtime workspace."
            )
        identity = RuntimeOutputJournalIdentity(
            workspace_path=execution.workspace_path,
            execution_id=execution_id,
            operation_id=output.operation_id,
            step_id=output.execution_step_id,
            sequence=output.sequence,
            execution_attempt_id=output.execution_attempt_id,
            fencing_token=output.fencing_token,
            runtime_target_id=output.runtime_target_id,
            runtime_session_id=output.runtime_session_id,
        )
        return ExecutionOutputContentDescriptor(
            execution_id=execution_id,
            output_id=output_id,
            representation_id=representation_id,
            runtime_type=execution.runtime_type,
            preferred_target_id=output.runtime_target_id,
            identity=identity,
            journal_id=output.journal_id,
            media_type=representation.media_type,
            size_bytes=representation.size_bytes,
            checksum_sha256=representation.checksum_sha256,
            complete=representation.complete,
        )

    async def read_chunk(
        self,
        descriptor: ExecutionOutputContentDescriptor,
        start: int,
        end_exclusive: int,
    ) -> RuntimeOutputContentChunk:
        try:
            chunk = await self._runtime_storage.read_output_content(
                descriptor.runtime_type,
                descriptor.preferred_target_id,
                descriptor.identity,
                journal_id=descriptor.journal_id,
                output_id=descriptor.output_id,
                representation_id=descriptor.representation_id,
                start=start,
                end_exclusive=end_exclusive,
            )
        except Exception as exc:
            raise ExecutionOutputContentUnavailableError(
                f"Execution Output representation "
                f"{descriptor.representation_id} is currently unavailable."
            ) from exc
        if (
            chunk.media_type != descriptor.media_type
            or chunk.size_bytes != descriptor.size_bytes
            or chunk.checksum_sha256 != descriptor.checksum_sha256
            or chunk.complete != descriptor.complete
            or chunk.start != start
            or chunk.end_exclusive != end_exclusive
            or len(chunk.content) != end_exclusive - start
        ):
            raise ExecutionOutputContentUnavailableError(
                "Runtime output content does not match PostgreSQL metadata."
            )
        if (
            start == 0
            and end_exclusive == descriptor.size_bytes
            and hashlib.sha256(chunk.content).hexdigest()
            != descriptor.checksum_sha256
        ):
            raise ExecutionOutputContentUnavailableError(
                "Runtime output content checksum verification failed."
            )
        return chunk

    async def stream(
        self,
        descriptor: ExecutionOutputContentDescriptor,
        start: int,
        end_exclusive: int,
    ) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._runtime_storage.stream_output_content(
                descriptor.runtime_type,
                descriptor.preferred_target_id,
                descriptor.identity,
                journal_id=descriptor.journal_id,
                output_id=descriptor.output_id,
                representation_id=descriptor.representation_id,
                start=start,
                end_exclusive=end_exclusive,
                expected_media_type=descriptor.media_type,
                expected_size_bytes=descriptor.size_bytes,
                expected_checksum_sha256=descriptor.checksum_sha256,
                expected_complete=descriptor.complete,
            ):
                yield chunk
        except Exception as exc:
            raise ExecutionOutputContentUnavailableError(
                f"Execution Output representation "
                f"{descriptor.representation_id} stream failed."
            ) from exc

    async def read_inline_text(
        self,
        descriptor: ExecutionOutputContentDescriptor,
        *,
        max_bytes: int,
    ) -> str | None:
        if descriptor.size_bytes > max_bytes or not _is_textual(
            descriptor.media_type
        ):
            return None
        chunk = await self.read_chunk(descriptor, 0, descriptor.size_bytes)
        try:
            return chunk.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ExecutionOutputContentUnavailableError(
                "Text output representation is not valid UTF-8."
            ) from exc


def _is_textual(media_type: str) -> bool:
    normalized = media_type.lower().split(";", 1)[0].strip()
    return (
        normalized.startswith("text/")
        or normalized
        in {
            "application/json",
            "application/javascript",
            "application/xml",
        }
        or normalized.endswith("+json")
        or normalized.endswith("+xml")
    )
