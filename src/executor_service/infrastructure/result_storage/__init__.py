"""Atomic shared Execution result storage."""

import asyncio
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID
from weakref import WeakValueDictionary

from executor_service.domain.results import (
    ExecutionSourceReference,
    StepResultAppend,
    StepResultDescriptor,
    StepResultIdentity,
    StepResultProjection,
    StepResultReference,
)
from executor_service.domain.runtime import RuntimeOutputRecord
from executor_service.infrastructure.result_storage.codec import (
    ResultOutputCodec,
)
from executor_service.infrastructure.result_storage.errors import (
    ResultStorageError,
)
from executor_service.infrastructure.result_storage.paths import (
    ResultStoragePaths,
)
from executor_service.infrastructure.result_storage.sources import (
    FilesystemExecutionSourceStore,
)
from executor_service.infrastructure.result_storage.steps import (
    FilesystemStepResultStore,
    remove_partial_files,
)

# Source files of one Operation written at a time; bounded so a large
# Operation does not take the whole shared thread pool.
SOURCE_WRITE_CONCURRENCY = 8


class FilesystemExecutionResultStore:
    def __init__(self, root: Path) -> None:
        self._paths = ResultStoragePaths(root)
        self._sources = FilesystemExecutionSourceStore(self._paths)
        self._steps = FilesystemStepResultStore(
            self._paths,
            self._sources,
            ResultOutputCodec(self._paths),
        )
        # Weak values: a Step's lock goes away once no append, seal or
        # waiter holds it, so the map does not grow with every Step run.
        self._locks: WeakValueDictionary[str, asyncio.Lock] = (
            WeakValueDictionary()
        )
        self._locks_guard = asyncio.Lock()

    async def snapshot_source(
        self,
        execution_id: UUID,
        operation_id: UUID,
        step_id: UUID,
        content: str,
    ) -> ExecutionSourceReference:
        (reference,) = await self.snapshot_sources(
            execution_id, operation_id, [(step_id, content)]
        )
        return reference

    async def snapshot_sources(
        self,
        execution_id: UUID,
        operation_id: UUID,
        sources: Sequence[tuple[UUID, str]],
    ) -> list[ExecutionSourceReference]:
        """Snapshot one Operation's ``(step_id, content)`` pairs, in order."""
        limit = asyncio.Semaphore(SOURCE_WRITE_CONCURRENCY)

        async def write(
            step_id: UUID, content: str
        ) -> ExecutionSourceReference:
            async with limit:
                return await asyncio.to_thread(
                    self._sources.write,
                    execution_id,
                    operation_id,
                    step_id,
                    content,
                )

        references = await asyncio.gather(
            *(write(step_id, content) for step_id, content in sources)
        )
        if references:
            await asyncio.to_thread(
                self._sources.sync_directory, execution_id, operation_id
            )
        return references

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
            lock = self._locks.get(key)
            if lock is None:
                lock = self._locks[key] = asyncio.Lock()
            return lock


def remove_partial_result(root: Path, identity: StepResultIdentity) -> None:
    """Maintenance helper; the caller must prove the fence is inactive."""

    remove_partial_files(root, identity)


__all__ = [
    "FilesystemExecutionResultStore",
    "FilesystemExecutionSourceStore",
    "FilesystemStepResultStore",
    "ResultOutputCodec",
    "ResultStorageError",
    "ResultStoragePaths",
    "remove_partial_files",
    "remove_partial_result",
]
