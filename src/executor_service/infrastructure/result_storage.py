"""Public facade for atomic shared Execution result storage."""

import asyncio
from pathlib import Path
from uuid import UUID

from executor_service.domain.results import (
    ExecutionSourceReference,
    StepResultAppend,
    StepResultDescriptor,
    StepResultIdentity,
    StepResultProjection,
    StepResultReference,
)
from executor_service.domain.runtime import RuntimeOutputRecord
from executor_service.infrastructure._result_storage import (
    FilesystemExecutionSourceStore,
    FilesystemStepResultStore,
    ResultOutputCodec,
    ResultStorageError,
    ResultStoragePaths,
    remove_partial_files,
)


class FilesystemExecutionResultStore:
    def __init__(self, root: Path) -> None:
        self._paths = ResultStoragePaths(root)
        self._sources = FilesystemExecutionSourceStore(self._paths)
        self._steps = FilesystemStepResultStore(
            self._paths,
            self._sources,
            ResultOutputCodec(),
        )
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def snapshot_source(
        self,
        execution_id: UUID,
        step_id: UUID,
        content: str,
    ) -> ExecutionSourceReference:
        return await asyncio.to_thread(
            self._sources.snapshot,
            execution_id,
            step_id,
            content,
        )

    async def read_source(self, reference: ExecutionSourceReference) -> str:
        return await asyncio.to_thread(self._sources.read, reference)

    async def read_step_outputs(
        self, reference: StepResultReference
    ) -> list[dict[str, object]]:
        projection = await self.read_step_projection(reference)
        return projection.outputs

    async def read_step_projection(
        self, reference: StepResultReference
    ) -> StepResultProjection:
        return await asyncio.to_thread(self._steps.read_projection, reference)

    async def begin_step_result(
        self,
        identity: StepResultIdentity,
        source: ExecutionSourceReference,
    ) -> None:
        async with await self._lock(identity):
            await asyncio.to_thread(self._steps.begin, identity, source)

    async def append_step_outputs(
        self,
        identity: StepResultIdentity,
        *,
        expected_offset: int,
        batch_id: UUID,
        records: tuple[RuntimeOutputRecord, ...],
    ) -> StepResultAppend:
        async with await self._lock(identity):
            return await asyncio.to_thread(
                self._steps.append,
                identity,
                expected_offset,
                batch_id,
                records,
            )

    async def finalize_step_result(
        self,
        identity: StepResultIdentity,
        *,
        execution_count: int | None,
        error_message: str | None = None,
    ) -> StepResultDescriptor:
        async with await self._lock(identity):
            return await asyncio.to_thread(
                self._steps.seal,
                identity,
                "FAILED" if error_message else "FINALIZED",
                execution_count,
                error_message,
            )

    async def abort_step_result(
        self,
        identity: StepResultIdentity,
        *,
        reason: str,
    ) -> StepResultDescriptor:
        async with await self._lock(identity):
            return await asyncio.to_thread(
                self._steps.seal,
                identity,
                "ABORTED",
                None,
                reason,
            )

    async def _lock(self, identity: StepResultIdentity) -> asyncio.Lock:
        key = self._paths.result_relative(identity).as_posix()
        async with self._locks_guard:
            return self._locks.setdefault(key, asyncio.Lock())


def remove_partial_result(root: Path, identity: StepResultIdentity) -> None:
    """Maintenance helper; the caller must prove the fence is inactive."""

    remove_partial_files(root, identity)


__all__ = [
    "FilesystemExecutionResultStore",
    "ResultStorageError",
    "remove_partial_result",
]
