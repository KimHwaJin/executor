"""Public facade for Runtime-backed Artifact discovery and persistence."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.domain.enums import ArtifactStatus, ArtifactType
from executor_service.domain.runtime import (
    RuntimeStorage,
    RuntimeStorageSnapshot,
)
from executor_service.infrastructure._artifacts import (
    ARTIFACT_DIRECTORY_TYPES,
    ArtifactDescriptor,
    ArtifactDiscovery,
    ArtifactManifestEntry,
    ArtifactPersistence,
    runtime_file_descriptor,
)
from executor_service.infrastructure.execution_leases import ExecutionLease
from executor_service.infrastructure.workspace import ExecutionWorkspace


class ExecutionArtifactManager:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        self._discovery = ArtifactDiscovery()
        self._persistence = ArtifactPersistence(session_factory)

    async def snapshot(
        self, driver: RuntimeStorage, workspace: ExecutionWorkspace
    ) -> RuntimeStorageSnapshot:
        return await driver.artifact_snapshot(workspace.runtime_relative_path)

    async def discover_and_register(
        self,
        *,
        driver: RuntimeStorage,
        workspace: ExecutionWorkspace,
        before: RuntimeStorageSnapshot,
        lease: ExecutionLease,
        sequence: int,
        status: ArtifactStatus,
        allow_cancel_requested: bool = False,
    ) -> list[UUID]:
        descriptors = await self._discovery.discover(
            driver, workspace, before, status
        )
        return await self._persistence.persist(
            descriptors,
            lease=lease,
            sequence=sequence,
            allow_cancel_requested=allow_cancel_requested,
        )

    async def register_notebook(
        self,
        *,
        driver: RuntimeStorage,
        workspace: ExecutionWorkspace,
        lease: ExecutionLease,
        sequence: int,
    ) -> list[UUID]:
        metadata = await driver.file_metadata(workspace.notebook_path)
        descriptor = runtime_file_descriptor(
            metadata,
            ArtifactType.NOTEBOOK,
            ArtifactStatus.AVAILABLE,
            metadata={},
        )
        return await self._persistence.persist(
            [descriptor],
            lease=lease,
            sequence=sequence,
        )


__all__ = [
    "ARTIFACT_DIRECTORY_TYPES",
    "ArtifactDescriptor",
    "ArtifactManifestEntry",
    "ExecutionArtifactManager",
]
